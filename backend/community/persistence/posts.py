"""community_posts 仓储（方案 §7.2）。

列表排序契约（§8.2）：latest = pinned DESC, last_activity_at DESC, post_id DESC；
unanswered 额外过滤 solved_reply_id IS NULL。keyset 分页使用行值比较
(pinned, last_activity_at, post_id)，cursor sort_key 与之一一对应（§8.2 绑定项）。
hidden/deleted 不进入公共列表；详情按墓碑契约返回（§6.6）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

#: 列表查询返回的字段集（不含 body：列表只含 active 帖子，正文不进列表载荷）
_POST_LIST_COLUMNS = (
    "p.post_id, p.user_id, p.author_display_name, p.board_id, b.slug, b.name, "
    "b.description, p.title, p.pinned, p.discussion_status, p.solved_reply_id, "
    "p.reply_count, p.like_count, p.created_at, p.last_activity_at"
)


async def list_posts(
    session: AsyncSession,
    *,
    board_id: UUID | None,
    sort: str,
    after: tuple[bool, Any, UUID] | None,
    limit: int,
) -> list[dict[str, Any]]:
    """默认列表（§8.2）：status=active；latest/unanswered；keyset 分页。

    after = (pinned, last_activity_at, post_id) 为最后一条记录的完整排序 key；
    None 表示首页。
    """
    where = ["p.status = 'active'"]
    params: dict[str, Any] = {"limit": limit + 1}
    if board_id is not None:
        where.append("p.board_id = :board_id")
        params["board_id"] = board_id
    if sort == "unanswered":
        where.append("p.solved_reply_id IS NULL")
    if after is not None:
        where.append("(p.pinned, p.last_activity_at, p.post_id) < (:a_pinned, :a_la, :a_id)")
        params.update({"a_pinned": after[0], "a_la": after[1], "a_id": after[2]})
    sql = (
        "SELECT "
        + _POST_LIST_COLUMNS
        + " FROM community_posts p JOIN community_boards b ON b.board_id = p.board_id "
        + "WHERE "
        + " AND ".join(where)
        + " ORDER BY p.pinned DESC, p.last_activity_at DESC, p.post_id DESC LIMIT :limit"
    )
    result = await session.execute(text(sql), params)
    return [dict(row) for row in result.mappings().all()]


async def liked_post_ids(session: AsyncSession, post_ids: list[UUID], user_id: UUID) -> set[UUID]:
    """当前用户已点赞的帖子 ID 集合（viewer_liked，§6.6）。"""
    if not post_ids:
        return set()
    result = await session.execute(
        text(
            "SELECT post_id FROM community_post_likes "
            "WHERE user_id = :user_id AND post_id = ANY(:post_ids)"
        ),
        {"user_id": user_id, "post_ids": list(post_ids)},
    )
    return {row[0] for row in result.all()}


async def get_post_any_status(session: AsyncSession, post_id: UUID) -> dict[str, Any] | None:
    """详情行（含 hidden/deleted）：可见性判断由 service 层完成（§8.4）。"""
    result = await session.execute(
        text(
            "SELECT p.post_id, p.user_id, p.author_display_name, p.board_id, b.slug, "
            "b.name, b.description, p.title, p.body, p.content_hash, p.pinned, "
            "p.discussion_status, "
            "p.solved_reply_id, p.solution_generation, p.reply_count, p.like_count, "
            "p.status, p.eligible_for_memory, p.created_at, p.updated_at, "
            "p.last_activity_at, p.deleted_at "
            "FROM community_posts p JOIN community_boards b ON b.board_id = p.board_id "
            "WHERE p.post_id = :post_id"
        ),
        {"post_id": post_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 写路径（PR-C：与 Outbox/幂等/通知在同一业务事务，由 service 编排）
# ---------------------------------------------------------------------------


async def insert_post(
    session: AsyncSession,
    *,
    post_id: UUID,
    user_id: UUID,
    author_display_name: str,
    board_id: UUID,
    title: str,
    body: str,
    content_hash: str,
) -> None:
    """插入帖子（§7.2：pinned 默认 false，MVP 仅预留管理员写入）。"""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    await session.execute(
        text(
            "INSERT INTO community_posts "
            "(post_id, user_id, author_display_name, board_id, title, body, "
            " content_hash, status, discussion_status, eligible_for_memory, pinned, "
            " created_at, updated_at, last_activity_at) "
            "VALUES (:post_id, :user_id, :name, :board_id, :title, :body, :hash, "
            " 'active', 'open', true, false, :now, :now, :now)"
        ),
        {
            "post_id": post_id,
            "user_id": user_id,
            "name": author_display_name,
            "board_id": board_id,
            "title": title,
            "body": body,
            "hash": content_hash,
            "now": now,
        },
    )


async def bump_reply_activity(session: AsyncSession, post_id: UUID) -> None:
    """新回复：reply_count + 1、last_activity_at 更新（D30：仅回复创建更新活动）。"""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    await session.execute(
        text(
            "UPDATE community_posts SET reply_count = reply_count + 1, "
            "    last_activity_at = :now, updated_at = :now "
            "WHERE post_id = :post_id"
        ),
        {"post_id": post_id, "now": now},
    )


async def decrement_reply_count(session: AsyncSession, post_id: UUID) -> None:
    """删除回复：reply_count - 1（D26：恒为当前 active 回复数）。"""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    await session.execute(
        text(
            "UPDATE community_posts SET reply_count = GREATEST(reply_count - 1, 0), "
            "    updated_at = :now "
            "WHERE post_id = :post_id"
        ),
        {"post_id": post_id, "now": now},
    )


async def mark_post_deleted(session: AsyncSession, post_id: UUID) -> None:
    """软删除帖子（§11.1：closed + eligible=false + deleted_at；不物理清除正文）。"""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    await session.execute(
        text(
            "UPDATE community_posts SET status = 'deleted', discussion_status = 'closed', "
            "    eligible_for_memory = false, deleted_at = :now, updated_at = :now "
            "WHERE post_id = :post_id"
        ),
        {"post_id": post_id, "now": now},
    )


async def set_solution(
    session: AsyncSession,
    post_id: UUID,
    *,
    reply_id: UUID | None,
    generation: int,
) -> None:
    """设置/切换/取消解决（§8.5：generation 由 service 决定是否递增）。"""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    await session.execute(
        text(
            "UPDATE community_posts SET solved_reply_id = :reply_id, "
            "    solution_generation = :generation, updated_at = :now "
            "WHERE post_id = :post_id"
        ),
        {"post_id": post_id, "reply_id": reply_id, "generation": generation, "now": now},
    )
