"""内部账号删除接口（规格 §19.7 / §13.16 / §15.9）。

仅允许账户服务使用非浏览器服务 JWT（actor_type=system）+ memory:maintenance
scope 调用；目标用户只能经 account_identity_mappings 解析，调用方不得直接
注入内部 user_id。幂等锚点为 account_deletion_id（派生 operation 幂等键）。
"""

from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends, Response

from backend.auth.context import SCOPE_MEMORY_MAINTENANCE, AuthContext
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
            resolver = IdentityMappingRepository(session)
            user_id = await resolver.resolve(
                issuer=request.issuer, external_subject=request.external_subject
            )
            if user_id is None:
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
                if row is None:  # pragma: no cover - manifest 与 operation 同事务创建
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

            input_kind, priority = OPERATION_ROUTING["purge_account_memory"]
            operation_id = uuid4()
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
