"""Gateway API 依赖注入与共享写路径（规格 §14.2 / §18 / §19 / §4.5 / §19.9）。

- 认证：CompositeAuthVerifier（dev/production 适配器）输出统一 AuthContext。
- 授权：scope + actor 权限矩阵（§18.2/§18.3）实现为 Depends 工厂。
- 写路径：Idempotency-Key 校验 → canonical hash → insert_operation →
  P0/P1 经共享 claim_operation 领取后由 MemoryGraphRunner 快速执行（最多 2 秒）。
- cursor：§19.9 HMAC 签名不透明 cursor 的签发/校验助手。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.auth.context import AuthContext
from backend.memory.contracts.commands import MemoryPayload
from backend.memory.contracts.common import (
    OPERATION_ROUTING,
    PRIORITY_P1,
    TERMINAL_STATUSES,
    idempotency_payload_hash,
    new_trace_id,
    user_privacy_hash,
)
from backend.memory.contracts.errors import (
    AccountPurgeInProgressError,
    CursorExpiredError,
    CursorInvalidError,
    DatabaseUnavailableError,
    IdempotencyKeyReusedError,
    InvalidIdempotencyKeyError,
    PublicError,
    RateLimitedError,
)
from backend.memory.contracts.operations import (
    GraphStateChangeView,
    MemoryOperation,
    MemoryOperationResult,
    MutationResult,
)
from backend.memory.graph.runner import MemoryGraphRunner
from backend.memory.maintenance_gate import MaintenanceGate
from backend.memory.persistence import operations as ops_repo
from backend.memory.persistence.database import Database
from backend.memory.services.memory_service import MemoryService
from backend.memory.worker.checkpoint import thread_id_for_operation
from backend.memory.worker.worker import Worker
from backend.settings import Settings

# D24/D29 共享化：认证依赖、cursor 签发、固定窗口限流实现位于 backend/shared，
# 本模块保留 re-export 与错误映射，既有引用点不受影响。
from backend.shared.auth_context import (
    BREAK_GLASS_HEADER as BREAK_GLASS_HEADER,
)
from backend.shared.auth_context import (
    get_auth_context as get_auth_context,
)
from backend.shared.auth_context import (
    require as require,
)
from backend.shared.cursor import CursorError as CursorError
from backend.shared.cursor import issue_cursor as issue_cursor
from backend.shared.cursor import resolve_cursor as _shared_resolve_cursor
from backend.shared.ratelimit import FixedWindowRateLimiter as FixedWindowRateLimiter

#: P0/P1 快速路径同步等待窗口（§14.2 第 3–4 条）
FAST_PATH_TIMEOUT_SECONDS = 2.0

logger = logging.getLogger("memory.api")


@dataclass
class ApiRuntime:
    """API 进程运行时依赖（装配模式同 worker/main.py，§14.2）。"""

    settings: Settings
    session_factory: async_sessionmaker[AsyncSession]
    memory_service: MemoryService
    runner: MemoryGraphRunner
    gateway_worker: Worker
    db: Database | None = None
    maintenance_gate: MaintenanceGate | None = None
    #: 快速路径超时后继续执行的后台任务强引用（§14.2 第 6 条：不取消 Runner）
    background_tasks: set[asyncio.Task[None]] = field(default_factory=set)


# ---------------------------------------------------------------------------
# 基础依赖
# ---------------------------------------------------------------------------


def get_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_runtime(request: Request) -> ApiRuntime:
    runtime: ApiRuntime | None = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise DatabaseUnavailableError("API 运行时尚未初始化")
    return runtime


# ---------------------------------------------------------------------------
# trace_id（§4.1：继承 W3C Trace Context，否则生成 32 位十六进制）
# ---------------------------------------------------------------------------

_TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")


def trace_id_from_headers(headers: Any) -> str:
    traceparent = headers.get("traceparent")
    if traceparent:
        match = _TRACEPARENT_RE.match(traceparent.strip().lower())
        if match and match.group(1) != "0" * 32:
            return match.group(1)
    return new_trace_id()


def get_trace_id(request: Request) -> str:
    trace_id: str | None = getattr(request.state, "trace_id", None)
    return trace_id or new_trace_id()


# ---------------------------------------------------------------------------
# 认证与授权（§18.1–§18.3）
# D29：get_auth_context / require 实现已提取至 backend/shared/auth_context.py，
# 本模块从 shared 导入（见顶部 import），保持既有 API 签名与引用点兼容。
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 进程内固定窗口限流（§18.5）
# D12/D24：FixedWindowRateLimiter 实现已提取至 backend/shared/ratelimit.py，
# 支持 window_seconds 参数（默认 60），本模块从 shared 导入。
# ---------------------------------------------------------------------------

#: 限流 bucket → settings 字段（§18.5：写 30/min、搜索 60/min、图谱标记 30/min）
_BUCKET_SETTING = {
    "write": "rate_limit_write_per_minute",
    "search": "rate_limit_search_per_minute",
    "graph_state": "rate_limit_graph_state_per_minute",
}


def rate_limit(bucket: str) -> Any:
    """限流依赖工厂；以认证上下文 user_id 为 key（禁止放进指标/日志明文）。"""

    async def _dep(request: Request, auth: AuthContext = Depends(get_auth_context)) -> None:
        settings = get_settings(request)
        limit = int(getattr(settings, _BUCKET_SETTING[bucket]))
        limiter: FixedWindowRateLimiter = request.app.state.rate_limiter
        if not limiter.hit(bucket, str(auth.user_id), limit):
            raise RateLimitedError("请求超过限流阈值，请稍后重试")

    return _dep


# ---------------------------------------------------------------------------
# Idempotency-Key（§4.5 / §19）
# ---------------------------------------------------------------------------

_IDEMPOTENCY_KEY_RE = re.compile(r"^[\x21-\x7e]{1,200}$")


async def require_idempotency_key(request: Request) -> str:
    """所有写请求必填；ASCII 可见字符、长度 1–200、不允许控制字符（§4.5）。"""
    key = request.headers.get("idempotency-key")
    if key is None or not _IDEMPOTENCY_KEY_RE.match(key):
        raise InvalidIdempotencyKeyError(
            "缺少或非法的 Idempotency-Key（ASCII 可见字符，1–200）", field="Idempotency-Key"
        )
    return key


# ---------------------------------------------------------------------------
# 写路径：operation 创建 + 幂等 + P0/P1 快速执行（§5.3 / §6.6 / §14.2）
# ---------------------------------------------------------------------------


async def submit_operation(
    runtime: ApiRuntime,
    *,
    auth: AuthContext,
    payload: MemoryPayload,
    public_hash_input: dict[str, Any],
    idempotency_key: str,
    trace_id: str,
) -> dict[str, Any]:
    """持久化 operation 并按优先级尝试快速路径；返回最新 operation 行。

    - operation_type/input_kind/priority 由 payload.kind 推导（§5.3）。
    - 幂等冲突时 hash 不同 → IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD（§4.5）。
    - P0/P1：Gateway 经共享 claim_operation 领取，Runner 最多等 2 秒（§14.2）。
    """
    from backend.memory import metrics

    kind = payload.kind
    input_kind, priority = OPERATION_ROUTING[kind]
    operation_id = uuid4()
    operation = MemoryOperation(
        operation_id=operation_id,
        idempotency_key=idempotency_key,
        user_id=auth.user_id,
        actor_type=auth.actor_type,
        input_kind=input_kind,
        operation_type=kind,
        priority=priority,
        occurred_at=datetime.now(UTC),
        payload=payload,
        trace_id=trace_id,
        graph_thread_id=thread_id_for_operation(operation_id),
    )
    payload_hash = idempotency_payload_hash(public_hash_input)
    async with runtime.session_factory() as session:
        async with session.begin():
            # §21.3 步骤 1：账号删除 manifest 存在（任意状态）时阻止新 operation
            from backend.memory.persistence import account_deletion as deletion_repo

            user_hash = user_privacy_hash(runtime.settings.privacy_hmac_key, str(auth.user_id))
            if await deletion_repo.get_manifest_by_user_hash(session, user_hash=user_hash):
                raise AccountPurgeInProgressError("账号删除进行中，已阻止新 operation")
            inserted = await ops_repo.insert_operation(
                session, operation, idempotency_payload_hash=payload_hash
            )
            if not inserted:
                existing = await ops_repo.get_by_idempotency(
                    session,
                    user_id=auth.user_id,
                    actor_type=auth.actor_type,
                    idempotency_key=idempotency_key,
                )
                if existing is None:  # pragma: no cover - 同事务插入冲突后必可读
                    raise DatabaseUnavailableError("幂等冲突后未能读取原 operation")
                if existing["idempotency_payload_hash"] != payload_hash:
                    raise IdempotencyKeyReusedError("同一幂等键提交了不同的 payload")
                return existing
    metrics.memory_operations_total.labels(type=kind, status="queued").inc()
    if priority >= PRIORITY_P1:
        await _try_fast_path(runtime, operation_id)
    async with runtime.session_factory() as session:
        row = await ops_repo.get_operation(session, operation_id)
    if row is None:  # pragma: no cover - 刚插入的行必存在
        raise DatabaseUnavailableError("operation 创建后读取失败")
    return row


async def _try_fast_path(runtime: ApiRuntime, operation_id: UUID) -> None:
    """Gateway 快速路径（§14.2）。

    未领取到 → 立即返回，由 Worker 按同一 claim 规则接管；已领取但 2 秒内
    未完成 → 后台任务继续执行并续约 Lease，HTTP 断开不取消 Runner。
    """
    # HTTP middleware 虽已覆盖本请求，后台快速路径可能在响应返回后才继续执行；
    # 因此领取动作自身也必须经 maintenance gate，不能在恢复开始后抢到 Lease。
    gate = runtime.maintenance_gate
    if gate is not None:
        async with gate.traffic():
            return await _try_fast_path_claimed(runtime, operation_id)
    return await _try_fast_path_claimed(runtime, operation_id)


async def _try_fast_path_claimed(runtime: ApiRuntime, operation_id: UUID) -> None:
    """在已通过 maintenance gate 后领取并启动 Gateway 快速路径。"""
    worker = runtime.gateway_worker
    async with runtime.session_factory() as session:
        async with session.begin():
            claimed = await ops_repo.claim_operation(
                session,
                worker_id=worker.worker_id,
                lease_seconds=worker.config.lease_seconds,
                operation_id=operation_id,
            )
    if not claimed:
        return
    task = asyncio.create_task(worker.execute_claimed(claimed[0]))
    runtime.background_tasks.add(task)
    task.add_done_callback(runtime.background_tasks.discard)
    await asyncio.wait({task}, timeout=FAST_PATH_TIMEOUT_SECONDS)


# ---------------------------------------------------------------------------
# 结果渲染（§7.1 / §7.2）
# ---------------------------------------------------------------------------


def operation_result_from_row(row: dict[str, Any]) -> MemoryOperationResult:
    """memory_operations 行 → 公开 MemoryOperationResult。"""
    stored = row.get("result") or {}
    raw_error = row.get("public_error")
    error: PublicError | None = None
    if raw_error:
        error = PublicError(
            code=str(raw_error.get("code", "INTERNAL_ERROR")),
            message=str(raw_error.get("message", "")),
            retryable=bool(raw_error.get("retryable", False)),
            field=raw_error.get("field"),
            trace_id=str(row["trace_id"]),
        )
    status = str(row["status"])
    completed_at = row.get("completed_at")
    return MemoryOperationResult(
        operation_id=UUID(str(row["operation_id"])),
        status=status,  # type: ignore[arg-type]
        operation_type=str(row["operation_type"]),  # type: ignore[arg-type]
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=completed_at,
        # memory_operations 无独立 cancelled_at 列，cancelled 时以 completed_at 呈现（§11.6）
        cancelled_at=completed_at if status == "cancelled" else None,
        mutations=[MutationResult.model_validate(m) for m in stored.get("mutations", [])],
        review_candidate_ids=[UUID(str(c)) for c in stored.get("review_candidate_ids", [])],
        graph_state_changes=[
            GraphStateChangeView.model_validate(c) for c in stored.get("graph_state_changes", [])
        ],
        warnings=list(stored.get("warnings", [])),
        error=error,
    )


def status_code_for_row(row: dict[str, Any]) -> int:
    """§7.2：完成（终态）200，未完成 202。"""
    return 200 if str(row["status"]) in TERMINAL_STATUSES else 202


# ---------------------------------------------------------------------------
# 不透明 cursor（§19.9）
# D24：issue_cursor 实现位于 backend/shared/cursor.py（含 D13 bind_principal 扩展），
# 本模块 re-export；resolve_cursor 保留 CursorError → Memory 公共错误码的映射。
# ---------------------------------------------------------------------------


def resolve_cursor(
    settings: Settings,
    token: str,
    *,
    route: str,
    user_id: UUID,
    filters: dict[str, Any],
    bind_principal: bool = True,
) -> dict[str, Any]:
    """验签并绑定路由/主体/筛选/过期；返回 payload（含 sort_key）。

    D13：公共游标（帖子列表/详情回复分页）传 bind_principal=False，
    payload 不绑定 principal_hash。
    """
    try:
        return _shared_resolve_cursor(
            settings,
            token,
            route=route,
            user_id=user_id,
            filters=filters,
            bind_principal=bind_principal,
        )
    except CursorError as exc:
        if exc.code == "CURSOR_EXPIRED":
            raise CursorExpiredError(str(exc)) from exc
        raise CursorInvalidError(str(exc)) from exc
