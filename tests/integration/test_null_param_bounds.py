"""空筛选/空 cursor 参数边界集成测试（§12.1 / §19.8）。

回归锁定第 16 步验收发现的两个 500：空 list 过滤与 None cursor 经
psycopg 绑定为 unknown 类型，PostgreSQL 无法推断多态类型。
"""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.persistence import index_entries, notifications


async def test_search_candidates_with_empty_filters(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """topic_keys/memory_types 为空列表（API 默认）时必须正常返回而不是 500。"""
    async with session_factory() as session:
        hits = await index_entries.search_candidates(
            session,
            user_id=uuid4(),
            query="极限",
            topic_keys=[],
            memory_types=[],
            min_similarity=0.2,
            limit=20,
        )
    assert hits == []


async def test_list_notifications_with_null_cursor(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """cursor_created_at/cursor_id 为 None（首页请求）时必须正常返回。"""
    async with session_factory() as session:
        page = await notifications.list_notifications(
            session,
            user_id=uuid4(),
            limit=20,
            cursor_created_at=None,
            cursor_id=None,
            unread_only=False,
        )
    assert page == []
