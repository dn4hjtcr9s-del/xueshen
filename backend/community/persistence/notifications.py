"""community_notifications 仓储（方案 §7.7，v1.6 冻结）。

- 通知与触发操作在同一事务写入；dedupe_key UNIQUE + ON CONFLICT DO NOTHING（D33）；
- 去重公式（§7.7）：post_replied:{post_id}:{reply_id}、
  reply_marked_solved:{post_id}:{reply_id}:{solution_generation}；
- 只查询当前认证用户记录；read 幂等且不能跨用户（§8.6）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount


def dedupe_key(
    event_type: str,
    *,
    post_id: UUID | None = None,
    reply_id: UUID | None = None,
    solution_generation: int | None = None,
    application_id: UUID | None = None,
) -> str:
    """§7.7/§7.8 去重公式（冻结）。"""
    if event_type == "post_replied":
        assert reply_id is not None and post_id is not None
        return f"post_replied:{post_id}:{reply_id}"
    if event_type == "reply_marked_solved":
        assert reply_id is not None and post_id is not None and solution_generation is not None
        return f"reply_marked_solved:{post_id}:{reply_id}:{solution_generation}"
    if event_type in ("application_approved", "application_rejected"):
        assert application_id is not None
        return f"{event_type}:{application_id}"
    raise ValueError(f"未知通知事件类型: {event_type}")


async def insert_notification(
    session: AsyncSession,
    *,
    notification_id: UUID,
    recipient_user_id: UUID,
    actor_user_id: UUID,
    event_type: str,
    post_id: UUID | None,
    reply_id: UUID | None,
    board_slug: str | None,
    title: str,
    body: str,
    dedupe: str,
) -> bool:
    """插入通知；同 dedupe_key 已存在返回 False（D33：ON CONFLICT DO NOTHING）。"""
    rowcount = await exec_rowcount(
        session,
        text(
            "INSERT INTO community_notifications "
            "(notification_id, recipient_user_id, actor_user_id, event_type, "
            " post_id, reply_id, board_slug, title, body, dedupe_key) "
            "VALUES (:nid, :recipient, :actor, :event_type, :post_id, :reply_id, "
            " :board_slug, :title, :body, :dedupe) "
            "ON CONFLICT (dedupe_key) DO NOTHING"
        ),
        {
            "nid": notification_id,
            "recipient": recipient_user_id,
            "actor": actor_user_id,
            "event_type": event_type,
            "post_id": post_id,
            "reply_id": reply_id,
            "board_slug": board_slug,
            "title": title,
            "body": body,
            "dedupe": dedupe,
        },
    )
    return rowcount == 1


async def list_notifications(
    session: AsyncSession,
    *,
    user_id: UUID,
    unread_only: bool,
    after: tuple[Any, UUID] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """通知列表（§8.6）：created_at DESC, notification_id DESC keyset。"""
    params: dict[str, Any] = {"user_id": user_id, "limit": limit + 1}
    where = "WHERE recipient_user_id = :user_id"
    if unread_only:
        where += " AND read_at IS NULL"
    if after is not None:
        where += " AND (created_at, notification_id) < (:a_created, :a_id)"
        params.update({"a_created": after[0], "a_id": after[1]})
    result = await session.execute(
        text(
            "SELECT notification_id, recipient_user_id, actor_user_id, event_type, "
            "post_id, reply_id, board_slug, title, body, read_at, created_at "
            "FROM community_notifications "
            + where
            + " ORDER BY created_at DESC, notification_id DESC LIMIT :limit"
        ),
        params,
    )
    return [dict(row) for row in result.mappings().all()]


async def unread_count(session: AsyncSession, *, user_id: UUID) -> int:
    result = await session.execute(
        text(
            "SELECT COUNT(*) FROM community_notifications "
            "WHERE recipient_user_id = :user_id AND read_at IS NULL"
        ),
        {"user_id": user_id},
    )
    return int(result.scalar_one())


async def mark_read(session: AsyncSession, *, user_id: UUID, notification_id: UUID) -> bool:
    """标记单条已读（幂等；仅限收件人本人，§8.6）。"""
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE community_notifications SET read_at = now() "
                "WHERE notification_id = :nid AND recipient_user_id = :user_id "
                "  AND read_at IS NULL"
            ),
            {"nid": notification_id, "user_id": user_id},
        )
        == 1
    )


async def mark_all_read(session: AsyncSession, *, user_id: UUID) -> int:
    """全部已读（§8.6/D14：只更新当前认证用户的未读记录）。"""
    return await exec_rowcount(
        session,
        text(
            "UPDATE community_notifications SET read_at = now() "
            "WHERE recipient_user_id = :user_id AND read_at IS NULL"
        ),
        {"user_id": user_id},
    )
