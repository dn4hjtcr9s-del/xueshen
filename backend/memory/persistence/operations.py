"""memory_operations 仓储（规格 §13.2 / §11 / §14.2）。

Gateway 与 Worker 复用同一个 claim_operation（FOR UPDATE SKIP LOCKED）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.contracts.common import max_attempts_for_priority
from backend.memory.contracts.errors import OperationCancelNotAllowedError
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.persistence.database import exec_rowcount

INSERT_SQL = text(
    """
    INSERT INTO memory_operations (
        operation_id, user_id, actor_type, input_kind, operation_type,
        idempotency_key, idempotency_payload_hash, priority, status,
        payload, result, public_error, trace_id, graph_thread_id,
        occurred_at, max_attempts
    ) VALUES (
        :operation_id, :user_id, :actor_type, :input_kind, :operation_type,
        :idempotency_key, :idempotency_payload_hash, :priority, 'queued',
        CAST(:payload AS jsonb), NULL, NULL, :trace_id, :graph_thread_id,
        :occurred_at, :max_attempts
    )
    ON CONFLICT ON CONSTRAINT uq_memory_operation_idempotency DO NOTHING
    """
)


async def insert_operation(
    session: AsyncSession, operation: MemoryOperation, *, idempotency_payload_hash: str
) -> bool:
    """插入 operation；幂等冲突时返回 False（调用方应读取原 operation）。"""
    rowcount = await exec_rowcount(
        session,
        INSERT_SQL,
        {
            "operation_id": operation.operation_id,
            "user_id": operation.user_id,
            "actor_type": operation.actor_type,
            "input_kind": operation.input_kind,
            "operation_type": operation.operation_type,
            "idempotency_key": operation.idempotency_key,
            "idempotency_payload_hash": idempotency_payload_hash,
            "priority": operation.priority,
            "payload": json.dumps(operation.payload.model_dump(mode="json"), ensure_ascii=False),
            "trace_id": operation.trace_id,
            "graph_thread_id": operation.graph_thread_id,
            "occurred_at": operation.occurred_at,
            "max_attempts": max_attempts_for_priority(operation.priority),
        },
    )
    return rowcount == 1


async def get_by_idempotency(
    session: AsyncSession, *, user_id: UUID, actor_type: str, idempotency_key: str
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT * FROM memory_operations "
            "WHERE user_id = :user_id AND actor_type = :actor_type "
            "AND idempotency_key = :idempotency_key"
        ),
        {"user_id": user_id, "actor_type": actor_type, "idempotency_key": idempotency_key},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def get_operation(session: AsyncSession, operation_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM memory_operations WHERE operation_id = :operation_id"),
        {"operation_id": operation_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def claim_operation(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    operation_id: UUID | None = None,
    batch_size: int = 10,
) -> list[dict[str, Any]]:
    """领取 operation 并设置 Lease，必须在同一数据库事务中完成（§14.2）。

    operation_id 为 None 时按 priority DESC, created_at ASC 批量领取。
    """
    if operation_id is not None:
        select_sql = text(
            """
            SELECT operation_id FROM memory_operations
            WHERE operation_id = :operation_id
              AND status IN ('queued', 'retry_wait')
              AND next_run_at <= now()
            FOR UPDATE SKIP LOCKED
            """
        )
        params: dict[str, Any] = {"operation_id": operation_id}
    else:
        select_sql = text(
            """
            SELECT operation_id FROM memory_operations
            WHERE status IN ('queued', 'retry_wait')
              AND next_run_at <= now()
            ORDER BY priority DESC, created_at ASC
            LIMIT :batch_size
            FOR UPDATE SKIP LOCKED
            """
        )
        params = {"batch_size": batch_size}
    rows = (await session.execute(select_sql, params)).scalars().all()
    if not rows:
        return []
    lease_expires = datetime.now(UTC) + timedelta(seconds=lease_seconds)
    await session.execute(
        text(
            """
            UPDATE memory_operations
            SET status = 'running', locked_by = :worker_id,
                lease_expires_at = :lease_expires, started_at = now(),
                last_heartbeat_at = now(), attempt_count = attempt_count + 1,
                updated_at = now()
            WHERE operation_id = ANY(:ids)
            """
        ),
        {"worker_id": worker_id, "lease_expires": lease_expires, "ids": list(rows)},
    )
    claimed: list[dict[str, Any]] = []
    for op_id in rows:
        row = await get_operation(session, op_id)
        if row:
            claimed.append(row)
    return claimed


async def heartbeat(
    session: AsyncSession, *, operation_id: UUID, worker_id: str, lease_seconds: int
) -> bool:
    rowcount = await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_operations
            SET last_heartbeat_at = now(),
                lease_expires_at = :lease_expires, updated_at = now()
            WHERE operation_id = :operation_id AND locked_by = :worker_id
              AND status = 'running'
            """
        ),
        {
            "operation_id": operation_id,
            "worker_id": worker_id,
            "lease_expires": datetime.now(UTC) + timedelta(seconds=lease_seconds),
        },
    )
    return rowcount == 1


async def mark_commit_started(session: AsyncSession, *, operation_id: UUID) -> None:
    """进入 commit 副作用前打标记（§11.6 取消仲裁）；仅对 running 行生效。

    必须在独立短事务中调用，与提交事务分离：进程在 commit 中崩溃时标记残留，
    由 Lease 回收后执行层在下次开始执行时清除（clear_commit_started）。
    """
    await session.execute(
        text(
            "UPDATE memory_operations SET commit_started_at = now(), updated_at = now() "
            "WHERE operation_id = :operation_id AND status = 'running'"
        ),
        {"operation_id": operation_id},
    )


async def clear_commit_started(session: AsyncSession, *, operation_id: UUID) -> None:
    """清除 commit 标记：提交事务结束后、或执行开始时清理崩溃残留（§11.6）。"""
    await session.execute(
        text(
            "UPDATE memory_operations SET commit_started_at = NULL, updated_at = now() "
            "WHERE operation_id = :operation_id AND commit_started_at IS NOT NULL"
        ),
        {"operation_id": operation_id},
    )


async def complete_operation(
    session: AsyncSession,
    *,
    operation_id: UUID,
    status: str,
    result: dict[str, Any] | None,
    public_error: dict[str, Any] | None,
    llm_call_count: int = 0,
) -> None:
    await session.execute(
        text(
            """
            UPDATE memory_operations
            SET status = :status, result = CAST(:result AS jsonb),
                public_error = CAST(:public_error AS jsonb),
                completed_at = now(), updated_at = now(),
                locked_by = NULL, lease_expires_at = NULL,
                commit_started_at = NULL,
                llm_call_count = llm_call_count + :llm_call_count
            WHERE operation_id = :operation_id
            """
        ),
        {
            "operation_id": operation_id,
            "status": status,
            "result": json.dumps(result, ensure_ascii=False) if result is not None else None,
            "public_error": (
                json.dumps(public_error, ensure_ascii=False) if public_error is not None else None
            ),
            "llm_call_count": llm_call_count,
        },
    )


async def reschedule_operation(
    session: AsyncSession, *, operation_id: UUID, next_run_at: datetime, status: str
) -> None:
    """任务级重试退避后重新排队（§11.2）。"""
    await session.execute(
        text(
            """
            UPDATE memory_operations
            SET status = :status, next_run_at = :next_run_at, updated_at = now(),
                locked_by = NULL, lease_expires_at = NULL, commit_started_at = NULL
            WHERE operation_id = :operation_id
            """
        ),
        {"operation_id": operation_id, "status": status, "next_run_at": next_run_at},
    )


async def recover_expired_leases(session: AsyncSession) -> int:
    """Scheduler 回收过期 Lease：running → retry_wait（§14.3）。"""
    rowcount = await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_operations
            SET status = 'retry_wait', next_run_at = now(), updated_at = now(),
                locked_by = NULL, lease_expires_at = NULL
            WHERE status = 'running' AND lease_expires_at < now()
            """
        ),
    )
    return rowcount


async def request_cancel(session: AsyncSession, *, operation_id: UUID) -> dict[str, Any] | None:
    """取消规则（§11.6）。返回更新后的行；不可取消返回 None（调用方区分 409）。"""
    row = await get_operation(session, operation_id)
    if row is None:
        return None
    status = row["status"]
    if status in ("queued", "retry_wait"):
        await session.execute(
            text(
                """
                UPDATE memory_operations
                SET status = 'cancelled', completed_at = now(), updated_at = now(),
                    locked_by = NULL, lease_expires_at = NULL
                WHERE operation_id = :operation_id
                """
            ),
            {"operation_id": operation_id},
        )
    elif status == "running":
        if row.get("commit_started_at") is not None:
            # 已进入 commit 副作用，不允许取消（§11.6，裁决 2026-08-11）
            raise OperationCancelNotAllowedError(
                "operation 已进入 commit，不允许取消", field="status"
            )
        # 协作取消：Runner 在节点入口/commit 前检查 cancel_requested_at
        await session.execute(
            text(
                "UPDATE memory_operations SET cancel_requested_at = now(), "
                "updated_at = now() WHERE operation_id = :operation_id"
            ),
            {"operation_id": operation_id},
        )
    elif status == "needs_review":
        await session.execute(
            text(
                """
                UPDATE memory_operations
                SET status = 'cancelled', completed_at = now(), updated_at = now()
                WHERE operation_id = :operation_id
                """
            ),
            {"operation_id": operation_id},
        )
    else:
        return None
    return await get_operation(session, operation_id)


async def get_cancel_requested(session: AsyncSession, operation_id: UUID) -> bool:
    result = await session.execute(
        text(
            "SELECT cancel_requested_at IS NOT NULL FROM memory_operations "
            "WHERE operation_id = :operation_id"
        ),
        {"operation_id": operation_id},
    )
    return bool(result.scalar())


async def list_user_operations(
    session: AsyncSession, *, user_id: UUID, operation_id: UUID
) -> dict[str, Any] | None:
    """用户只能访问自己的 operation（§19.3）。"""
    result = await session.execute(
        text(
            "SELECT * FROM memory_operations "
            "WHERE operation_id = :operation_id AND user_id = :user_id"
        ),
        {"operation_id": operation_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None
