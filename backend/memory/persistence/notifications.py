"""用户通知仓储（规格 §13.13 / §19.6）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount


async def insert_notification(
    session: AsyncSession,
    *,
    user_id: UUID,
    event_type: str,
    title: str,
    body: str,
    aggregate_type: str,
    aggregate_id: str,
    source_outbox_id: UUID,
) -> dict[str, Any] | None:
    """UNIQUE(source_outbox_id) 保证至少一次投递下不重复（§13.13）。"""
    result = await session.execute(
        text(
            """
            INSERT INTO memory_user_notifications (
                notification_id, user_id, event_type, title, body,
                aggregate_type, aggregate_id, source_outbox_id
            ) VALUES (
                :notification_id, :user_id, :event_type, :title, :body,
                :aggregate_type, :aggregate_id, :source_outbox_id
            )
            ON CONFLICT (source_outbox_id) DO NOTHING
            RETURNING *
            """
        ),
        {
            "notification_id": uuid4(),
            "user_id": user_id,
            "event_type": event_type,
            "title": title,
            "body": body,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "source_outbox_id": source_outbox_id,
        },
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def list_notifications(
    session: AsyncSession,
    *,
    user_id: UUID,
    limit: int,
    cursor_created_at: datetime | None,
    cursor_id: UUID | None,
    unread_only: bool,
) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            """
            SELECT * FROM memory_user_notifications
            WHERE user_id = :user_id
              AND (:unread_only = false OR read_at IS NULL)
              AND (
                    CAST(:cursor_created_at AS timestamptz) IS NULL
                    OR (created_at, notification_id) < (
                        CAST(:cursor_created_at AS timestamptz),
                        CAST(:cursor_id AS uuid)
                    )
                  )
            ORDER BY created_at DESC, notification_id DESC
            LIMIT :limit
            """
        ),
        {
            "user_id": user_id,
            "limit": limit,
            "cursor_created_at": cursor_created_at,
            "cursor_id": cursor_id,
            "unread_only": unread_only,
        },
    )
    return [dict(r) for r in result.mappings().all()]


async def unread_count(session: AsyncSession, *, user_id: UUID) -> int:
    result = await session.execute(
        text(
            "SELECT COUNT(*) FROM memory_user_notifications "
            "WHERE user_id = :user_id AND read_at IS NULL"
        ),
        {"user_id": user_id},
    )
    return int(result.scalar_one())


async def mark_read(
    session: AsyncSession, *, user_id: UUID, notification_id: UUID
) -> dict[str, Any] | None:
    """幂等已读：重复调用返回同一 read_at（§19.6）。"""
    result = await session.execute(
        text(
            """
            UPDATE memory_user_notifications
            SET read_at = COALESCE(read_at, now())
            WHERE notification_id = :notification_id AND user_id = :user_id
            RETURNING *
            """
        ),
        {"notification_id": notification_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def mark_all_read(session: AsyncSession, *, user_id: UUID) -> int:
    """全部已读（D14：只更新当前认证用户的未读记录；幂等）。"""
    return await exec_rowcount(
        session,
        text(
            "UPDATE memory_user_notifications SET read_at = now() "
            "WHERE user_id = :user_id AND read_at IS NULL"
        ),
        {"user_id": user_id},
    )


async def purge_older_than(session: AsyncSession, *, cutoff: datetime, batch_size: int) -> int:
    """清理超过 90 天的通知；不影响 Outbox delivery 和最小审计（§13.13）。"""
    rowcount = await exec_rowcount(
        session,
        text(
            """
            DELETE FROM memory_user_notifications
            WHERE notification_id IN (
                SELECT notification_id FROM memory_user_notifications
                WHERE created_at < :cutoff
                LIMIT :batch_size
            )
            """
        ),
        {"cutoff": cutoff, "batch_size": batch_size},
    )
    return rowcount
