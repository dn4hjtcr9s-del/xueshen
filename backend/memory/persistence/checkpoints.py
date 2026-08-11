"""Checkpoint 清理适配器（规格 §11.4）。

优先使用 Checkpointer 官方删除 API；若当前依赖版本只能直接 SQL，
表名/prefix/namespace 依据实际安装版本确认，并用集成测试固定兼容行为。
步骤 10（Worker/Scheduler）接入完整实现。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount

#: langgraph-checkpoint-postgres 3.0.4 表名前缀
CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def graph_thread_id_for(operation_id: UUID) -> str:
    return f"memory-op:{operation_id}"


async def delete_checkpoints_for_thread(session: AsyncSession, thread_id: str) -> int:
    """删除指定 graph thread 的 checkpoint 数据。返回删除的 checkpoints 行数。"""
    await session.execute(
        text("DELETE FROM checkpoint_writes WHERE thread_id = :thread_id"),
        {"thread_id": thread_id},
    )
    await session.execute(
        text("DELETE FROM checkpoint_blobs WHERE thread_id = :thread_id"),
        {"thread_id": thread_id},
    )
    rowcount = await exec_rowcount(
        session,
        text("DELETE FROM checkpoints WHERE thread_id = :thread_id"),
        {"thread_id": thread_id},
    )
    return rowcount


async def list_terminal_threads_older_than(
    session: AsyncSession, *, cutoff: datetime, batch_size: int
) -> list[str]:
    """找出 terminal operation 且超过保留期的 graph thread。

    terminal 7 天、needs_review/dead_letter 30 天由调用方按状态分别计算 cutoff。
    不得删除 running/Lease 未过期 operation 的 Checkpoint（§11.4）。
    """
    result = await session.execute(
        text(
            """
            SELECT graph_thread_id FROM memory_operations
            WHERE status IN ('succeeded', 'needs_review', 'dead_letter', 'cancelled')
              AND completed_at < :cutoff
            LIMIT :batch_size
            """
        ),
        {"cutoff": cutoff, "batch_size": batch_size},
    )
    return [str(r[0]) for r in result.all()]
