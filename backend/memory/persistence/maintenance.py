"""memory_maintenance_runs 仓储（规格 §13.14 / §14.3）。

Scheduler 先创建或复用 maintenance run（带幂等键），它是调度幂等、
batch cursor 和维护任务总状态的唯一真相；只有需要进入 MemoryManagerGraph
的 batch 才创建 memory_operations 并通过 operation_id 关联。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount


async def create_or_reuse_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    maintenance_type: str,
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    """先创建或复用 maintenance run（§14.3）。返回 (行, 是否新建)。"""
    rowcount = await exec_rowcount(
        session,
        text(
            """
            INSERT INTO memory_maintenance_runs (
                run_id, maintenance_type, idempotency_key, status
            ) VALUES (
                :run_id, :maintenance_type, :idempotency_key, 'queued'
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            """
        ),
        {
            "run_id": run_id,
            "maintenance_type": maintenance_type,
            "idempotency_key": idempotency_key,
        },
    )
    row = await get_run_by_key(session, idempotency_key=idempotency_key)
    assert row is not None  # 同事务内 INSERT/SELECT 必可见
    return row, rowcount == 1


async def get_run_by_key(session: AsyncSession, *, idempotency_key: str) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM memory_maintenance_runs WHERE idempotency_key = :key"),
        {"key": idempotency_key},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def attach_operation(session: AsyncSession, *, run_id: UUID, operation_id: UUID) -> None:
    """关联进入 Graph 的 batch operation（§14.3）。"""
    await session.execute(
        text(
            "UPDATE memory_maintenance_runs SET operation_id = :operation_id WHERE run_id = :run_id"
        ),
        {"operation_id": operation_id, "run_id": run_id},
    )


async def complete_run(
    session: AsyncSession,
    *,
    run_id: UUID,
    status: str,
    cursor: str | None,
    result: dict[str, Any] | None,
) -> None:
    """直接由 Scheduler 收尾的 run（不经过 Graph 的任务，如通知清理/备份检查）。"""
    await session.execute(
        text(
            """
            UPDATE memory_maintenance_runs
            SET status = :status, cursor = :cursor,
                result = CAST(:result AS jsonb),
                started_at = COALESCE(started_at, now()), completed_at = now()
            WHERE run_id = :run_id
            """
        ),
        {
            "run_id": run_id,
            "status": status,
            "cursor": cursor,
            "result": json.dumps(result, ensure_ascii=False) if result is not None else None,
        },
    )


async def update_run_by_operation(
    session: AsyncSession,
    *,
    operation_id: UUID,
    status: str,
    cursor: str | None,
    result: dict[str, Any],
) -> None:
    """Graph 分支 persist_cursor_or_finish：按 operation_id 回写 run（§10.7 / §14.3）。

    status='running' 表示 batch 未完成、cursor 待 Scheduler 调度下一批。
    """
    await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_maintenance_runs
            SET status = :status, cursor = :cursor,
                result = CAST(:result AS jsonb),
                started_at = COALESCE(started_at, now()),
                completed_at = CASE WHEN :status IN ('succeeded', 'failed')
                                    THEN now() ELSE NULL END
            WHERE operation_id = :operation_id
            """
        ),
        {
            "operation_id": operation_id,
            "status": status,
            "cursor": cursor,
            "result": json.dumps(result, ensure_ascii=False),
        },
    )


async def mark_run_busy_by_operation(
    session: AsyncSession,
    *,
    operation_id: UUID,
    result: dict[str, Any],
) -> None:
    """busy 分支（§14.3 / 复审 P3）：只保持 running 状态与结果，**不触碰 cursor**。

    修复前 busy 分支回写 cursor=payload.cursor（旧值），若持有锁的实例刚提交了
    新 cursor，busy 写会后落地并把 cursor 回退，导致同批次重跑（last-writer-wins）。
    保持 cursor 不动：run 的进度由持锁实例的更新决定，Scheduler 按当前 cursor
    续排，幂等键兜底重复批次。

    status 守卫（复审 Optional）：仅当 run 仍处于 queued/running 时才置 running——
    若 busy 写恰好落在持锁实例提交最终状态（succeeded/failed）之后，本更新变为
    no-op，避免把已收尾的 run 回退成 running 导致整轮按 initial 重排。
    """
    await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_maintenance_runs
            SET status = 'running',
                result = CAST(:result AS jsonb),
                started_at = COALESCE(started_at, now())
            WHERE operation_id = :operation_id
              AND status IN ('queued', 'running')
            """
        ),
        {
            "operation_id": operation_id,
            "result": json.dumps(result, ensure_ascii=False),
        },
    )


async def count_dead_letters(session: AsyncSession) -> dict[str, int]:
    """dead letter 指标检查（§14.3）。"""
    result = await session.execute(
        text(
            """
            SELECT
                (SELECT COUNT(*) FROM memory_operations WHERE status = 'dead_letter')
                    AS operations,
                (SELECT COUNT(*) FROM memory_outbox WHERE status = 'dead_letter')
                    AS outbox
            """
        )
    )
    row = result.mappings().one()
    return {"operations": int(row["operations"]), "outbox": int(row["outbox"])}


async def list_document_user_ids(session: AsyncSession, *, batch_size: int) -> list[UUID]:
    """有记忆文档的用户（孤立版本清理按用户进入 Graph，§6.5）。"""
    result = await session.execute(
        text(
            "SELECT DISTINCT user_id FROM memory_documents ORDER BY user_id ASC LIMIT :batch_size"
        ),
        {"batch_size": batch_size},
    )
    return [UUID(str(r[0])) for r in result.all()]


async def has_successful_backup_since(session: AsyncSession, *, since: datetime) -> bool:
    """当天是否已有成功备份（§14.3：Scheduler 只读 backup_runs 并告警）。"""
    result = await session.execute(
        text(
            "SELECT EXISTS("
            "SELECT 1 FROM backup_runs WHERE status = 'succeeded' AND started_at >= :since)"
        ),
        {"since": since},
    )
    return bool(result.scalar_one())
