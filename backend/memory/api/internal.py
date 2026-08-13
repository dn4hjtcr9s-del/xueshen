"""内部账号删除接口（规格 §19.7 / §13.16 / §15.9）。

仅允许账户服务使用非浏览器服务 JWT（actor_type=system）+ memory:maintenance
scope 调用；目标用户只能经 account_identity_mappings 解析，调用方不得直接
注入内部 user_id。幂等锚点为 account_deletion_id（派生 operation 幂等键）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.auth.context import (
    SCOPE_MEMORY_MAINTENANCE,
    SCOPE_MEMORY_SOURCE_DELETE,
    AuthContext,
)
from backend.memory.api.dependencies import (
    ApiRuntime,
    get_runtime,
    get_trace_id,
    operation_result_from_row,
    require,
    status_code_for_row,
)
from backend.memory.contracts.commands import AccountMemoryPurgeRequest, MaintenanceCommand
from backend.memory.contracts.common import (
    OPERATION_ROUTING,
    idempotency_payload_hash,
    user_privacy_hash,
)
from backend.memory.contracts.errors import (
    AccountPurgeAlreadyRunningError,
    DatabaseUnavailableError,
    IdentityMappingNotFoundError,
)
from backend.memory.contracts.events import AccountMemoryPurgeRequestedPayload
from backend.memory.contracts.evidence import SourceDeletedEvent
from backend.memory.contracts.operations import MemoryOperation, MemoryOperationResult
from backend.memory.persistence import account_deletion as deletion_repo
from backend.memory.persistence import operations as ops_repo
from backend.memory.persistence import outbox as outbox_repo
from backend.memory.persistence.identity import IdentityMappingRepository
from backend.memory.worker.checkpoint import thread_id_for_operation

router = APIRouter(prefix="/api/v1/internal", tags=["internal"])

_SERVICE_ONLY = frozenset({"system"})

#: 账号删除物理清理时限（§25：24 小时内）；备份副本随 30 天周期淘汰（§21.4）
_PURGE_DEADLINE = timedelta(hours=24)
_BACKUP_RETENTION = timedelta(days=30)


def _purge_idempotency_key(request: AccountMemoryPurgeRequest) -> str:
    return f"account-purge:{request.account_deletion_id}"


def _purge_operation_id(request: AccountMemoryPurgeRequest) -> UUID:
    """确定性 operation_id：purge 完成后自身行被物理删除，幂等重放需可重建。"""
    return uuid5(NAMESPACE_URL, f"memory:account-purge:{request.account_deletion_id}")


def _completed_purge_result(
    request: AccountMemoryPurgeRequest, manifest: dict[str, Any], response: Response
) -> MemoryOperationResult:
    """purge 完成后自身 operation 行已物理删除（§13.16）：合成终态结果。"""
    completed_at = manifest["purge_completed_at"]
    response.status_code = 200
    return MemoryOperationResult(
        operation_id=_purge_operation_id(request),
        status="succeeded",
        operation_type="purge_account_memory",
        created_at=manifest["requested_at"],
        updated_at=completed_at,
        completed_at=completed_at,
    )


@router.post("/account-memory/purge", response_model=MemoryOperationResult)
async def purge_account_memory(
    request: AccountMemoryPurgeRequest,
    response: Response,
    auth: AuthContext = Depends(require(actors=_SERVICE_ONLY, scope=SCOPE_MEMORY_MAINTENANCE)),
    runtime: ApiRuntime = Depends(get_runtime),
    trace_id: str = Depends(get_trace_id),
) -> MemoryOperationResult:
    """创建 purge_account_memory operation + manifest + Outbox（同一事务）。"""
    settings = runtime.settings
    idempotency_key = _purge_idempotency_key(request)
    async with runtime.session_factory() as session:
        async with session.begin():
            # purge 完成后身份映射已物理删除：先按 account_deletion_id 兜底定位
            manifest_by_id = await deletion_repo.get_manifest_by_id(
                session, account_deletion_id=request.account_deletion_id
            )
            resolver = IdentityMappingRepository(session)
            user_id = await resolver.resolve(
                issuer=request.issuer, external_subject=request.external_subject
            )
            if user_id is None:
                if manifest_by_id is not None and manifest_by_id["status"] == "completed":
                    return _completed_purge_result(request, manifest_by_id, response)
                raise IdentityMappingNotFoundError("身份映射不存在")

            user_hash = user_privacy_hash(settings.privacy_hmac_key, str(user_id))
            existing = await deletion_repo.get_manifest_by_user_hash(session, user_hash=user_hash)
            if existing is not None:
                if str(existing["account_deletion_id"]) != str(request.account_deletion_id):
                    raise AccountPurgeAlreadyRunningError("该用户已存在账号删除 manifest")
                # 幂等重放：返回原 operation 当前结果（§7.1）
                row = await ops_repo.get_by_idempotency(
                    session,
                    user_id=user_id,
                    actor_type="system",
                    idempotency_key=idempotency_key,
                )
                if row is None:
                    if existing["status"] == "completed":
                        return _completed_purge_result(request, existing, response)
                    raise DatabaseUnavailableError("manifest 存在但 purge operation 缺失")
                result = operation_result_from_row(row)
                response.status_code = status_code_for_row(row)
                return result

            inserted = await deletion_repo.insert_manifest(
                session,
                account_deletion_id=request.account_deletion_id,
                user_hash=user_hash,
                user_hash_key_version=settings.privacy_hmac_key_version,
                requested_at=request.requested_at,
                backup_retention_until=request.requested_at + _BACKUP_RETENTION,
            )
            if not inserted:  # pragma: no cover - 上面已检查；并发下唯一约束兜底
                raise AccountPurgeAlreadyRunningError("该用户已存在账号删除 manifest")
            # 写透 ops ledger（评审 P0-2）：恢复流程不 DROP ops schema
            await deletion_repo.upsert_ledger_entry(
                session,
                account_deletion_id=request.account_deletion_id,
                user_hash=user_hash,
                user_hash_key_version=settings.privacy_hmac_key_version,
                status="requested",
                requested_at=request.requested_at,
            )

            input_kind, priority = OPERATION_ROUTING["purge_account_memory"]
            operation_id = _purge_operation_id(request)
            operation = MemoryOperation(
                operation_id=operation_id,
                idempotency_key=idempotency_key,
                user_id=user_id,
                actor_type="system",
                input_kind=input_kind,
                operation_type="purge_account_memory",
                priority=priority,
                occurred_at=request.requested_at,
                payload=MaintenanceCommand(kind="purge_account_memory", target_user_id=user_id),
                trace_id=trace_id,
                graph_thread_id=thread_id_for_operation(operation_id),
            )
            await ops_repo.insert_operation(
                session,
                operation,
                idempotency_payload_hash=idempotency_payload_hash(request.model_dump(mode="json")),
            )
            event_payload = AccountMemoryPurgeRequestedPayload(
                account_deletion_id=request.account_deletion_id,
                user_hash=user_hash,
                requested_at=request.requested_at,
                purge_deadline=request.requested_at + _PURGE_DEADLINE,
            )
            await outbox_repo.insert_event(
                session,
                outbox_id=uuid4(),
                operation_id=operation_id,
                user_id=user_id,
                event_type="account_memory.purge_requested",
                aggregate_type="account_deletion",
                aggregate_id=str(request.account_deletion_id),
                aggregate_version=1,
                payload=event_payload.model_dump(mode="json"),
            )
            row = await ops_repo.get_operation(session, operation_id)
    if row is None:  # pragma: no cover - 同事务插入必可读
        raise DatabaseUnavailableError("purge operation 创建后读取失败")
    result = operation_result_from_row(row)
    response.status_code = status_code_for_row(row)
    return result


# ---------------------------------------------------------------------------
# 内部 source-deletions 端点（方案 §1.1 D5 / §8.6 / §22）
#
# 调用既有 RecordingSourceDeletionHandler 记录删除事实；只接受独立 system
# principal 的 memory:source_delete scope。未获 Memory/Conversation/Auth owner
# 批准前由部署配置决定是否挂载（默认关闭，见方案 §1.3）。
# ---------------------------------------------------------------------------


class SourceDeletionRequest(BaseModel):
    """内部删除事件请求（§8.6 步骤 4：source_ref/source_version 与 Reader 返回值一致）。

    修复（评审 P2）：event_id 由调用方稳定生成（幂等锚点），服务端不再随机生成——
    随机 event_id 使重试产生新记录，幂等失效。
    """

    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    user_id: UUID
    source_system: Literal["conversation", "activity"]
    source_ref: str = Field(min_length=1, max_length=500)
    source_version: str | None = Field(default=None, max_length=200)
    deleted_at: datetime | None = None


def build_source_deletions_router(
    *,
    handler: Any,
    session_factory: Any,
    enabled: bool = False,
) -> APIRouter | None:
    """组装内部 source-deletions Router（composition root 注入 RecordingSourceDeletionHandler）。

    修复（评审 P2）：enabled 默认 False（§1.3 默认关闭）——未获 owner 批准时
    端点不挂载。幂等锚点为调用方提供的 event_id，重复删除返回 duplicate。
    """
    if not enabled:
        return None
    deletion_router = APIRouter(prefix="/api/v1/internal", tags=["internal"])

    @deletion_router.post("/source-deletions")
    async def record_source_deletion(
        request: SourceDeletionRequest,
        auth: AuthContext = Depends(
            require(actors=_SERVICE_ONLY, scope=SCOPE_MEMORY_SOURCE_DELETE)
        ),
    ) -> dict[str, str]:
        """记录一条源删除事件（§8.6 步骤 4）。"""
        from datetime import UTC as _UTC

        event = SourceDeletedEvent(
            event_id=request.event_id,
            source_system=request.source_system,
            source_ref=request.source_ref,
            source_version=request.source_version,
            deleted_at=request.deleted_at or datetime.now(_UTC),
        )
        result = await handler.handle(user_id=request.user_id, event=event)
        return {"status": result}

    return deletion_router
