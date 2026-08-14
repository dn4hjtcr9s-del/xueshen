"""community_boards 仓储（方案 §7.1）。

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
            "SELECT board_id, slug, name, description, sort_order, status "
            "FROM community_boards WHERE status = 'active' "
            "ORDER BY sort_order, board_id"
        )
    )
    return [dict(row) for row in result.mappings().all()]


async def get_active_board(session: AsyncSession, board_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT board_id, slug, name, description, sort_order, status "
            "FROM community_boards WHERE board_id = :board_id AND status = 'active'"
        ),
        {"board_id": board_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_board_any_status(session: AsyncSession, board_id: UUID) -> dict[str, Any] | None:
    """内部使用：Publisher 校验板块存在与状态（§10.2：缺失/非 active 进 dead-letter）。"""
    result = await session.execute(
        text(
            "SELECT board_id, slug, name, description, sort_order, status "
            "FROM community_boards WHERE board_id = :board_id"
        ),
        {"board_id": board_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None
