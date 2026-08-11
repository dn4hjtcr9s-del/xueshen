"""Checkpoint 清理适配器（§11.4）。

- Graph thread 固定为 memory-op:{operation_id}。
- terminal operation 保留 7 天；needs_review/dead_letter 保留 30 天；账号删除 24 小时内清理。
- 使用 Checkpointer 官方删除 API（adelete_thread），不直接 SQL。
- 不得删除仍 running、Lease 未过期或正被执行的 operation Checkpoint。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

THREAD_PREFIX = "memory-op:"
TERMINAL_RETENTION_DAYS = 7
REVIEW_RETENTION_DAYS = 30
DEAD_LETTER_RETENTION_DAYS = 30


def thread_id_for_operation(operation_id: UUID) -> str:
    return f"{THREAD_PREFIX}{operation_id}"


class CheckpointCleanupAdapter:
    """封装 checkpointer 删除 API；表结构兼容由集成测试固定（§11.4 裁决）。"""

    def __init__(self, *, saver: Any) -> None:
        self._saver = saver

    async def setup(self) -> None:
        await self._saver.setup()

    async def delete_threads(self, thread_ids: list[str]) -> int:
        deleted = 0
        for thread_id in thread_ids:
            await self._saver.adelete_thread(thread_id)
            deleted += 1
        return deleted


async def list_expired_checkpoint_threads(
    session: AsyncSession, *, now: datetime, batch_size: int, cursor: str | None = None
) -> list[dict[str, Any]]:
    """到期且不在执行中的 operation 的 checkpoint thread（有界 batch + cursor，§10.7）。

    返回 {"thread_id", "cursor"} 列表；cursor 为 "{completed_at}:{operation_id}"，
    由调用方持久化后传入下一批，避免重复扫描已清理的 operation 行。
    """
    cursor_completed_at: datetime | None = None
    cursor_operation_id: UUID | None = None
    if cursor:
        raw_completed_at, _, raw_operation_id = cursor.rpartition(":")
        cursor_completed_at = datetime.fromisoformat(raw_completed_at)
        cursor_operation_id = UUID(raw_operation_id)
    result = await session.execute(
        text(
            """
            SELECT operation_id, status, completed_at FROM memory_operations
            WHERE (
                (status IN ('succeeded', 'cancelled')
                 AND completed_at < :terminal_cutoff)
                OR (status = 'needs_review' AND completed_at < :review_cutoff)
                OR (status = 'dead_letter' AND completed_at < :dead_cutoff)
            )
            AND NOT (
                status = 'running'
                AND lease_expires_at IS NOT NULL AND lease_expires_at > :now
            )
            AND (
                CAST(:cursor_completed_at AS timestamptz) IS NULL
                OR (completed_at, operation_id) > (:cursor_completed_at, :cursor_operation_id)
            )
            ORDER BY completed_at ASC, operation_id ASC
            LIMIT :batch_size
            """
        ),
        {
            "terminal_cutoff": now - timedelta(days=TERMINAL_RETENTION_DAYS),
            "review_cutoff": now - timedelta(days=REVIEW_RETENTION_DAYS),
            "dead_cutoff": now - timedelta(days=DEAD_LETTER_RETENTION_DAYS),
            "now": now,
            "cursor_completed_at": cursor_completed_at,
            "cursor_operation_id": cursor_operation_id,
            "batch_size": batch_size,
        },
    )
    return [
        {
            "thread_id": thread_id_for_operation(row.operation_id),
            "cursor": f"{row.completed_at.isoformat()}:{row.operation_id}",
        }
        for row in result.all()
    ]


async def list_account_checkpoint_threads(session: AsyncSession, *, user_id: UUID) -> list[str]:
    """账号删除：该用户全部 operation 的 checkpoint thread（§11.4）。"""
    result = await session.execute(
        text("SELECT operation_id FROM memory_operations WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    return [thread_id_for_operation(row.operation_id) for row in result.all()]
