"""Study 集成测试基础设施（方案 §20.3）。

- 数据库隔离：必须使用独立测试库 study_test（由 scripts/ci-local.sh
  backend-integration 创建、迁移并以 STUDY_DATABASE_URL 注入），
  **拒绝任何非测试库**；未配置时跳过（不影响其他域测试，Community 同规则）。
- 每个测试函数 TRUNCATE 全部 Study 用户数据表。
- API 测试用 dev auth（X-Dev-User-Id 模拟身份，§18.1 开发认证）；
  purge 内部端点用 X-Dev-Actor-Type/X-Dev-Scopes 模拟 system principal。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from backend.settings import Settings
from backend.study.persistence.database import (
    create_study_engine,
    create_study_session_factory,
)


def require_study_test_database(url: str) -> str:
    """校验 URL 指向 study 专用测试库；否则拒绝执行（§20.3，Community 同规则）。

    URL 为空 = 环境未启用 Study：跳过集成测试而非失败（本地无 study 库时
    不影响其他域测试；CI 由 scripts/ci-local.sh 注入 study_test）。
    """
    import re

    from sqlalchemy.engine import make_url

    if not url:
        pytest.skip("未配置 STUDY_DATABASE_URL，跳过 Study 集成测试")
    name = make_url(url).database or ""
    pattern = re.compile(r"^study_test(_\w+)?$")
    if not pattern.match(name):
        pytest.fail(
            f"Study 集成测试拒绝使用数据库 {name!r}（{url}）。"
            f"请通过 scripts/ci-local.sh backend-integration 运行，"
            f"或显式注入 study_test 数据库 URL。"
        )
    return name


#: Study 用户数据表（每测试清空）
STUDY_TABLES = (
    "study_account_purge_ledger",
    "study_scheduler_runs",
    "study_user_leases",
    "study_idempotency_requests",
    "study_outbox",
    "study_operations",
    "study_model_call_records",
    "study_daily_stats",
    "study_sessions",
    "study_daily_feed_items",
    "study_daily_feed_runs",
    "study_task_events",
    "study_tasks",
    "study_plan_revisions",
    "study_plan_availability",
    "study_plans",
    "study_plan_intakes",
)


@pytest.fixture(scope="session")
def _migrate_study() -> None:
    """幂等执行 study alembic upgrade head（CI 已执行时为 no-op）。"""
    from backend.settings import get_settings

    require_study_test_database(get_settings().study_database_url)
    command.upgrade(Config("study_alembic.ini"), "head")


@pytest.fixture()
def study_settings(tmp_path: Path) -> Settings:
    settings = Settings(app_env="test")
    require_study_test_database(settings.study_database_url)
    return settings


@pytest.fixture()
async def study_session_factory(
    study_settings: Settings,
    _migrate_study: None,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    require_study_test_database(study_settings.study_database_url)
    engine = create_study_engine(study_settings)
    factory = create_study_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            await session.execute(text(f"TRUNCATE {', '.join(STUDY_TABLES)} CASCADE"))
    yield factory
    await engine.dispose()


def _session_url(factory: async_sessionmaker[AsyncSession]) -> str:
    return str(factory.kw["bind"].url.render_as_string(hide_password=False))


def dev_settings_with_study(url: str, **overrides: object) -> Settings:
    """构造启用 Study 域的 development Settings（dev auth + 测试库 URL）。"""
    return Settings(
        app_env="development",
        dev_auth_enabled=True,
        dev_auth_allow_scope_override=True,
        study_domain_enabled=True,
        study_database_url=url,
        study_account_purge_service_token="test-purge-token",
        _env_file=None,
        **overrides,
    )


async def make_study_client(
    study_session_factory: async_sessionmaker[AsyncSession],
    **settings_overrides: object,
) -> AsyncClient:
    """装配带 Study 路由的 ASGI 测试客户端（不触发 lifespan，不连 Memory 库）。"""
    from backend.app import create_app

    settings = dev_settings_with_study(
        url=_session_url(study_session_factory), **settings_overrides
    )
    app = create_app(settings=settings)
    # dev auth 的 get_auth_context 读取 app.state.runtime.session_factory；
    # maintenance_gate=None 供 observability middleware 使用（Community 同模式）
    app.state.runtime = SimpleNamespace(
        session_factory=study_session_factory, maintenance_gate=None
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture()
async def client(
    study_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    async with await make_study_client(study_session_factory) as c:
        yield c


def auth(user_id: str) -> dict[str, str]:
    """dev auth：普通用户身份（X-Dev-User-Id）。"""
    return {"X-Dev-User-Id": user_id}


def system_auth() -> dict[str, str]:
    """dev auth：system principal + study:account_purge scope（purge 测试）。"""
    return {
        "X-Dev-User-Id": "00000000-0000-4000-8000-000000000000",
        "X-Dev-Actor-Type": "system",
        "X-Dev-Scopes": "study:account_purge",
    }


#: 测试用户（固定 UUID）
USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"


def manual_plan_body(
    *,
    start_date: str,
    target_date: str | None = None,
    duration_weeks: int | None = None,
    timezone: str = "Asia/Shanghai",
    blueprint: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Phase 1 manual 直录请求体（§8 示例同构）。"""
    return {
        "intent": {
            "goal": "六周内掌握线性代数基础",
            "start_date": start_date,
            "target_date": target_date,
            "duration_weeks": duration_weeks,
            "timezone": timezone,
            "weekly_availability": [
                {"day_of_week": 1, "available_minutes": 40},
                {"day_of_week": 3, "available_minutes": 40},
                {"day_of_week": 5, "available_minutes": 60},
            ],
            "session_min_minutes": 15,
            "session_max_minutes": 60,
            "preferences": [],
        },
        "generation_mode": "manual",
        "task_blueprint": blueprint
        or [
            {
                "title": "矩阵与线性方程组",
                "task_type": "learn",
                "estimated_minutes": 40,
                "topic_key": "linear-algebra:systems",
            }
        ],
    }
