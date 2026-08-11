"""数据库引擎与会话工厂（规格 §13.18 / §14.7）。

- 连接级 statement_timeout / lock_timeout 固定由 settings 注入。
- 所有 Memory 写入口使用同一用户级 advisory lock 实现，禁止各模块自行定义锁顺序。
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.settings import Settings

USER_LOCK_NAMESPACE = "memory-user-write:v1"


def user_lock_key(user_id: UUID) -> int:
    """固定 namespace + user_id 稳定 hash 的用户级 advisory lock key。"""
    digest = hashlib.sha256(f"{USER_LOCK_NAMESPACE}:{user_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_size=10,
        max_overflow=5,
        pool_pre_ping=True,
        connect_args={
            "options": (
                f"-c statement_timeout={settings.database_statement_timeout_ms} "
                f"-c lock_timeout={settings.database_lock_timeout_ms}"
            ),
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def acquire_user_lock(session: AsyncSession, user_id: UUID) -> None:
    """事务内用户级写锁；必须在锁定目标文档之前获取（§13.18）。"""
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": user_lock_key(user_id)}
    )


async def exec_rowcount(
    session: AsyncSession, sql: Any, params: dict[str, Any] | None = None
) -> int:
    """执行写语句并返回 rowcount（text() 的 Result 不暴露 rowcount 类型）。"""
    from sqlalchemy.engine import CursorResult

    result = await session.execute(sql, params or {})
    if isinstance(result, CursorResult):
        return result.rowcount
    return 0


class Database:
    """应用持有的数据库访问入口。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_engine(settings)
        self.session_factory = create_session_factory(self.engine)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def ping(self) -> bool:
        try:
            async with self.session_factory() as session:
                await session.execute(text("SELECT 1"))
            return True
        except OSError:
            return False

    async def close(self) -> None:
        await self.engine.dispose()
