"""Community 域持久化：独立 community 数据库引擎与会话工厂（方案 §4.2）。

独立 URL、连接池、迁移链（community_alembic.ini）。连接级 statement_timeout /
lock_timeout 复用现有 settings 的数据库超时配置（与 Conversation 同模式）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.settings import Settings


def create_community_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.community_database_url,
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


def create_community_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


class CommunityDatabase:
    """Community 域数据库访问入口（composition root 装配，§4.2/§13.1）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_community_engine(settings)
        self.session_factory = create_community_session_factory(self.engine)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def ping(self) -> bool:
        """readiness 探活：连接失败返回 False 而非抛异常（与 Conversation 同语义）。"""
        try:
            async with self.session_factory() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception as exc:
            from sqlalchemy.exc import DBAPIError, OperationalError

            if isinstance(exc, (DBAPIError, OperationalError, OSError)):
                return False
            raise

    async def close(self) -> None:
        await self.engine.dispose()
