"""集成测试基础设施：真实 PostgreSQL 容器 + 临时文件系统（§23.3）。

- 数据库使用 docker compose 的 PostgreSQL（settings 默认 127.0.0.1:55432/memory），
  与 ci-local.sh backend-integration 阶段一致；alembic upgrade head 幂等执行。
- 每个测试函数 TRUNCATE 全部用户数据表；图谱注册表（knowledge_graph_*）保留，
  其数据由 sync-knowledge-graph CLI 管理。
- Markdown 存储根使用 pytest tmp_path，测试间完全隔离。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from backend.memory.persistence.database import create_engine, create_session_factory
from backend.memory.services.memory_service import MemoryService
from backend.memory.storage.local_markdown import LocalMarkdownStore
from backend.settings import Settings

# 用户数据表（每测试清空）；图谱注册表 knowledge_graph_* 不在此列。
USER_TABLES = (
    "memory_privacy_audit_records",
    "account_deletion_manifest",
    "memory_break_glass_audit",
    "memory_break_glass_grants",
    "memory_llm_call_metrics",
    "memory_maintenance_runs",
    "backup_runs",
    "memory_user_notifications",
    "memory_internal_event_log",
    "memory_outbox_deliveries",
    "memory_outbox",
    "source_deletions",
    "graph_state_audit",
    "graph_user_node_activity",
    "graph_user_states",
    "memory_graph_links",
    "memory_deleted_evidence_suppressions",
    "memory_review_candidates",
    "memory_index_entries",
    "memory_commits",
    "memory_documents",
    "memory_operations",
    "account_identity_mappings",
)


@pytest.fixture(scope="session", autouse=True)
def _migrate() -> None:
    """幂等执行 alembic upgrade head（CI 已执行时此调用为 no-op）。"""
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(app_env="test", memory_storage_root=str(tmp_path / "storage"))


@pytest.fixture()
async def session_factory(
    settings: Settings,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    async with factory() as session:
        async with session.begin():
            await session.execute(text(f"TRUNCATE {', '.join(USER_TABLES)} CASCADE"))
    yield factory
    await engine.dispose()


@pytest.fixture()
def store(settings: Settings) -> LocalMarkdownStore:
    return LocalMarkdownStore(settings.memory_storage_root)


@pytest.fixture()
def memory_service(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
) -> MemoryService:
    return MemoryService(settings=settings, session_factory=session_factory, store=store)
