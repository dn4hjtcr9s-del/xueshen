"""Conversation 集成测试基础设施（方案 §26.3 / 附录 A.11 决策 1）。

与 Memory 集成测试同模式：真实 PostgreSQL 容器 + 独立测试库 conversation_test
（由 scripts/ci-local.sh backend-integration 创建、迁移并以
CONVERSATION_DATABASE_URL 注入），**拒绝任何非测试库**。
每个测试函数 TRUNCATE Conversation 全部用户数据表。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from backend.conversation.persistence.database import (
    create_conversation_engine,
    create_conversation_session_factory,
)
from backend.settings import Settings


def require_conversation_test_database(url: str) -> str:
    """校验 URL 指向 conversation 专用测试库；否则拒绝执行（评审 P1-1 同规则）。"""
    from sqlalchemy.engine import make_url

    name = make_url(url).database or ""
    pattern = re.compile(r"^conversation_test(_\w+)?$")
    if not pattern.match(name):
        pytest.fail(
            f"Conversation 集成测试拒绝使用数据库 {name!r}（{url}）。"
            f"请通过 scripts/ci-local.sh backend-integration 运行，"
            f"或显式注入 conversation_test 数据库 URL。"
        )
    return name


# Conversation 域用户数据表（每测试清空）
CONVERSATION_TABLES = (
    "conversation.conversation_jobs",
    "conversation.conversation_summaries",
    "conversation.conversation_outbox",
    "conversation.conversation_turn_events",
    "conversation.conversation_turns",
    "conversation.conversation_messages",
    "conversation.conversation_threads",
)


@pytest.fixture(scope="session")
def _migrate_conversation() -> None:
    """幂等执行 conversation alembic upgrade head（CI 已执行时为 no-op）。

    仅依赖数据库的集成测试显式请求本 fixture（通过 conversation_session_factory
    间接使用）；纯 Graph/单元测试（test_conversation_graph.py）不触发。
    """
    from backend.settings import get_settings

    require_conversation_test_database(get_settings().conversation_database_url)
    command.upgrade(Config("conversation_alembic.ini"), "head")


@pytest.fixture()
def conversation_settings(tmp_path: Path) -> Settings:
    settings = Settings(app_env="test")
    require_conversation_test_database(settings.conversation_database_url)
    return settings


@pytest.fixture()
async def conversation_session_factory(
    conversation_settings: Settings,
    _migrate_conversation: None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    require_conversation_test_database(conversation_settings.conversation_database_url)
    engine = create_conversation_engine(conversation_settings)
    factory = create_conversation_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            await session.execute(text(f"TRUNCATE {', '.join(CONVERSATION_TABLES)} CASCADE"))
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------------------
# 通用测试数据构造
# ---------------------------------------------------------------------------


def make_uuid(seed: int = 1) -> UUID:
    """确定性 UUID（测试可重放）。"""
    return UUID(int=seed)
