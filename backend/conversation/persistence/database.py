"""Conversation 数据库引擎与会话工厂（方案 §4.1 / §7）。

独立 conversation 数据库：独立 URL、连接池、迁移链（conversation_alembic.ini）。
连接级 statement_timeout / lock_timeout 复用现有 settings 的数据库超时配置。
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


def create_conversation_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.conversation_database_url,
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


def create_conversation_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


class ConversationDatabase:
    """Conversation 域数据库访问入口（composition root 装配）。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_conversation_engine(settings)
        self.session_factory = create_conversation_session_factory(self.engine)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    async def ping(self) -> bool:
        """readiness 探活：连接失败返回 False 而非抛异常。

        修复（第四轮必改）：连接失败抛的是 psycopg/sqlalchemy 的
        OperationalError（DBAPIError 子类），不会落到 OSError 分支——
        必须捕获 DBAPIError（含 OperationalError/InterfaceError），
        否则 /health/ready 在 conversation 库不可达时 500，且破坏
        无 DB 环境下的单元测试（期望 readiness 报告不可达而非崩溃）。
        """
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
