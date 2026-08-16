"""Study 域测试基础设施（方案 §20.3，Phase 0 骨架）。

- 数据库隔离：必须使用独立测试库 study_test（由 scripts/ci-local.sh
  backend-integration 创建、迁移并以 STUDY_DATABASE_URL 注入），
  **拒绝任何非测试库**，绝不 TRUNCATE 开发库数据。
- 每个测试函数 TRUNCATE 全部 Study 用户数据表。
- 集成测试在 Phase 1 起随 API/Repository 落地逐步接入本 conftest。
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy.engine import make_url


def require_test_database(url: str) -> str:
    """校验数据库 URL 指向 Study 链路的专用测试库；否则拒绝执行（§20.3）。"""
    name = make_url(url).database or ""
    pattern = re.compile(r"^study_test(_\w+)?$")
    if not pattern.match(name):
        pytest.fail(
            f"Study 集成测试拒绝使用数据库 {name!r}（{url}）。"
            f"请通过 scripts/ci-local.sh backend-integration 运行，或显式注入"
            f" study_test 数据库 URL。"
        )
    return name


#: Study 用户数据表（每测试清空；checkpoint 表由 Phase 2 加入）
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


@pytest.fixture(scope="session", autouse=True)
def _migrate() -> None:
    """幂等执行 study alembic upgrade head（CI 已执行时此调用为 no-op）。

    迁移前校验目标库是 study_test：禁止对开发库执行迁移。
    """
    from alembic.config import Config

    from alembic import command
    from backend.settings import get_settings

    require_test_database(get_settings().study_database_url)
    command.upgrade(Config("study_alembic.ini"), "head")
