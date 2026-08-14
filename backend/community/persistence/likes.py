"""community_post_likes 仓储（方案 §7.4）。

主键 (post_id, user_id) 保证唯一点赞；取消点赞采用物理删除关系行并在
同一事务中递减计数。点赞不是 Memory 证据（§7.4）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount


async def insert_like(session: AsyncSession, post_id: UUID, user_id: UUID) -> bool:
    """插入点赞行；已存在（重复点赞）返回 False 且不改计数。"""
    return (
        await exec_rowcount(
            session,
            text(
                "INSERT INTO community_post_likes (post_id, user_id) "
                "VALUES (:post_id, :user_id) ON CONFLICT DO NOTHING"
            ),
            {"post_id": post_id, "user_id": user_id},
        )
        == 1
    )


async def delete_like(session: AsyncSession, post_id: UUID, user_id: UUID) -> bool:
    """物理删除点赞行；不存在返回 False（取消点赞幂等，§8.5）。"""
    return (
        await exec_rowcount(
            session,
            text(
                "DELETE FROM community_post_likes WHERE post_id = :post_id AND user_id = :user_id"
            ),
            {"post_id": post_id, "user_id": user_id},
        )
        == 1
    )


async def bump_like_count(session: AsyncSession, post_id: UUID, delta: int) -> None:
    """事务内维护 like_count（±1，CHECK 约束防负）。"""
    now = datetime.now(UTC)
    await session.execute(
        text(
            "UPDATE community_posts SET like_count = like_count + :delta, "
            "updated_at = :now WHERE post_id = :post_id"
        ),
        {"post_id": post_id, "delta": delta, "now": now},
    )
