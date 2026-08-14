"""Community 集成测试基础设施（方案 §15.1 / 附录 A.11 决策 1）。

与 Conversation 集成测试同模式：真实 PostgreSQL 容器 + 独立测试库
community_test（由 scripts/ci-local.sh backend-integration 创建、迁移并以
COMMUNITY_DATABASE_URL 注入），**拒绝任何非测试库**（含未配置的情况，
因为 community_database_url 默认空，D25 语义下未注入即 fail）。
每个测试函数 TRUNCATE Community 全部用户数据表。
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
from backend.community.contracts.domain import BOARDS_SEED
from backend.community.persistence.database import (
    create_community_engine,
    create_community_session_factory,
)
from backend.settings import Settings


def require_community_test_database(url: str) -> str:
    """校验 URL 指向 community 专用测试库；否则拒绝执行（评审 P1-1 同规则）。

    URL 为空 = 环境未启用 Community（community_database_url 默认空，D25）：
    跳过集成测试而非失败，本地无社区库时不影响其他域测试。
    """
    from sqlalchemy.engine import make_url

    if not url:
        pytest.skip("未配置 COMMUNITY_DATABASE_URL，跳过 Community 集成测试")
    name = make_url(url).database or ""
    pattern = re.compile(r"^community_test(_\w+)?$")
    if not pattern.match(name):
        pytest.fail(
            f"Community 集成测试拒绝使用数据库 {name!r}（{url}）。"
            f"请通过 scripts/ci-local.sh backend-integration 运行，"
            f"或显式注入 community_test 数据库 URL。"
        )
    return name


# Community 域用户数据表（每测试清空）。community_boards 为迁移 seed 表
# （§7.1 固定 UUID 幂等写入），不参与 TRUNCATE；测试夹具复用 seed 数据。
COMMUNITY_TABLES = (
    "community_notifications",
    "community_idempotency_requests",
    "community_outbox",
    "community_post_likes",
    "community_replies",
    "community_posts",
)


@pytest.fixture(scope="session")
def _migrate_community() -> None:
    """幂等执行 community alembic upgrade head（CI 已执行时为 no-op）。

    仅依赖数据库的集成测试显式请求本 fixture（通过 community_session_factory
    间接使用）；纯单元测试不触发。
    """
    from backend.settings import get_settings

    require_community_test_database(get_settings().community_database_url)
    command.upgrade(Config("community_alembic.ini"), "head")


@pytest.fixture()
def community_settings(tmp_path: Path) -> Settings:
    settings = Settings(app_env="test")
    require_community_test_database(settings.community_database_url)
    return settings


@pytest.fixture()
async def community_session_factory(
    community_settings: Settings,
    _migrate_community: None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    require_community_test_database(community_settings.community_database_url)
    engine = create_community_engine(community_settings)
    factory = create_community_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            # community_posts/replies 与 boards 存在 FK 依赖，TRUNCATE CASCADE
            # 会连坐 community_boards，因此清空后按 §7.1 常量幂等重放 seed
            # （测试夹具复用迁移同一常量定义）。
            await session.execute(text(f"TRUNCATE {', '.join(COMMUNITY_TABLES)} CASCADE"))
            for board_id, slug, name, description, sort_order in BOARDS_SEED:
                await session.execute(
                    text(
                        "INSERT INTO community_boards "
                        "(board_id, slug, name, description, sort_order, status) "
                        "VALUES (:bid, :slug, :name, :description, :sort, 'active') "
                        "ON CONFLICT (slug) DO UPDATE SET "
                        "name = EXCLUDED.name, "
                        "description = EXCLUDED.description, "
                        "sort_order = EXCLUDED.sort_order, "
                        "status = 'active'"
                    ),
                    {
                        "bid": board_id,
                        "slug": slug,
                        "name": name,
                        "description": description,
                        "sort": sort_order,
                    },
                )
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------------------
# 通用测试数据构造
# ---------------------------------------------------------------------------


def make_uuid(seed: int = 1) -> UUID:
    """确定性 UUID（测试可重放）。"""
    return UUID(int=seed)
