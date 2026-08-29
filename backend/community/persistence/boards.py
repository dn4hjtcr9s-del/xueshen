"""community_boards 仓储（方案 §7.1 + community-rebuild-plan.md v3.9）。

公共端点只返回 status=active 的板块（§8.1）；hidden 板块不暴露。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def list_active_boards(session: AsyncSession) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT board_id, slug, name, description, sort_order, status, "
            "created_at, created_by, post_count "
            "FROM community_boards WHERE status = 'active' "
            "ORDER BY sort_order ASC, created_at ASC, board_id ASC"
        )
    )
    return [dict(row) for row in result.mappings().all()]


async def get_active_board(session: AsyncSession, board_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT board_id, slug, name, description, sort_order, status, "
            "created_at, created_by, post_count "
            "FROM community_boards WHERE board_id = :board_id AND status = 'active'"
        ),
        {"board_id": board_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_active_board_by_slug(session: AsyncSession, slug: str) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT board_id, slug, name, description, sort_order, status, "
            "created_at, created_by, post_count "
            "FROM community_boards WHERE slug = :slug AND status = 'active'"
        ),
        {"slug": slug},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_board_any_status(session: AsyncSession, board_id: UUID) -> dict[str, Any] | None:
    """内部使用：Publisher 校验板块存在与状态（§10.2：缺失/非 active 进 dead-letter）。"""
    result = await session.execute(
        text(
            "SELECT board_id, slug, name, description, sort_order, status, "
            "created_at, created_by, post_count "
            "FROM community_boards WHERE board_id = :board_id"
        ),
        {"board_id": board_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def insert_board(
    session: AsyncSession,
    *,
    board_id: UUID,
    slug: str,
    name: str,
    description: str,
    created_by: UUID,
    sort_order: int = 100,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO community_boards (
                board_id, slug, name, description, sort_order, status,
                created_by, post_count, created_at, updated_at
            ) VALUES (
                :board_id, :slug, :name, :description, :sort_order, 'active',
                :created_by, 0, now(), now()
            )
            """
        ),
        {
            "board_id": board_id,
            "slug": slug,
            "name": name,
            "description": description,
            "sort_order": sort_order,
            "created_by": created_by,
        },
    )


async def bump_post_count(session: AsyncSession, board_id: UUID, delta: int) -> None:
    await session.execute(
        text(
            """
            UPDATE community_boards
            SET post_count = post_count + :delta, updated_at = now()
            WHERE board_id = :board_id
            """
        ),
        {"board_id": board_id, "delta": delta},
    )


async def check_board_name_conflict(
    session: AsyncSession,
    name: str,
    slug: str,
) -> bool:
    """检查 boards 中是否已有同名/同 slug active 板块。"""
    result = await session.execute(
        text(
            """
            SELECT 1 FROM community_boards
            WHERE (lower(name) = lower(:name) OR slug = :slug)
              AND status = 'active'
            LIMIT 1
            """
        ),
        {"name": name, "slug": slug},
    )
    return result.scalar() is not None
