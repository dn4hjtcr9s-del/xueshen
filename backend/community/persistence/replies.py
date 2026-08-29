"""community_replies 仓储（方案 §7.3）。

回复默认按 created_at ASC, reply_id ASC 正序展示（§7.3），keyset 分页；
游标绑定具体 post_id（D39），防跨帖子复用。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession


async def list_replies_page(
    session: AsyncSession,
    *,
    post_id: UUID,
    after: tuple[Any, UUID] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """帖子详情内回复分页（§8.4）：created_at ASC, reply_id ASC。

    §9.4/§6.6：hidden 回复不通过公共 DTO 暴露（表现为 NOT_FOUND），
    仅返回 active（含 deleted 墓碑占位）。
    """
    params: dict[str, Any] = {
        "post_id": post_id,
        "limit": limit + 1,
    }
    where = "WHERE post_id = :post_id AND status IN ('active', 'deleted')"
    if after is not None:
        where += " AND (created_at, reply_id) > (:a_created, :a_id)"
        params.update({"a_created": after[0], "a_id": after[1]})
    result = await session.execute(
        text(
            "SELECT reply_id, post_id, user_id, author_display_name, body, content_hash, "
            "status, eligible_for_memory, created_at, updated_at, deleted_at "
            "FROM community_replies "
            + where
            + " ORDER BY created_at ASC, reply_id ASC LIMIT :limit"
        ),
        params,
    )
    return [dict(row) for row in result.mappings().all()]


async def get_reply_any_status(session: AsyncSession, reply_id: UUID) -> dict[str, Any] | None:
    """单条回复（含 hidden/deleted）：可见性判断由 service 层完成。"""
    result = await session.execute(
        text(
            "SELECT reply_id, post_id, user_id, author_display_name, body, content_hash, "
            "status, eligible_for_memory, created_at, updated_at, deleted_at "
            "FROM community_replies WHERE reply_id = :reply_id"
        ),
        {"reply_id": reply_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 写路径（PR-C）
# ---------------------------------------------------------------------------


async def insert_reply(
    session: AsyncSession,
    *,
    reply_id: UUID,
    post_id: UUID,
    user_id: UUID,
    author_display_name: str,
    body: str,
    content_hash: str,
) -> None:
    await session.execute(
        text(
            "INSERT INTO community_replies "
            "(reply_id, post_id, user_id, author_display_name, body, content_hash, "
            " status, eligible_for_memory) "
            "VALUES (:reply_id, :post_id, :user_id, :name, :body, :hash, "
            " 'active', true)"
        ),
        {
            "reply_id": reply_id,
            "post_id": post_id,
            "user_id": user_id,
            "name": author_display_name,
            "body": body,
            "hash": content_hash,
        },
    )


async def mark_reply_deleted(session: AsyncSession, reply_id: UUID) -> int:
    """软删除回复；返回影响行数（§7.17 #2 条件 UPDATE）。"""
    result = await session.execute(
        text(
            "UPDATE community_replies SET status = 'deleted', "
            "    eligible_for_memory = false, deleted_at = now(), updated_at = now() "
            "WHERE reply_id = :reply_id AND status = 'active'"
        ),
        {"reply_id": reply_id},
    )
    return int(result.rowcount or 0) if isinstance(result, CursorResult) else 0
