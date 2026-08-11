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
    "graph_activity_seen_events",
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


# ---------------------------------------------------------------------------
# Graph 集成测试设施（runtime context / runner）
# ---------------------------------------------------------------------------

import logging  # noqa: E402

from backend.memory.graph.openai_client import FakeMemoryLLMClient  # noqa: E402
from backend.memory.graph.runner import LocalLangGraphRunner  # noqa: E402
from backend.memory.graph.state import (  # noqa: E402
    MemoryRuntimeContext,
    SystemClock,
    SystemIdGenerator,
    default_registry_factory,
)
from backend.memory.readers.testing import (  # noqa: E402
    FakeActivityReader,
    FakeConversationReader,
)
from backend.memory.services.graph_state_service import (  # noqa: E402
    KnowledgeGraphStateService,
)


@pytest.fixture()
def fake_llm() -> FakeMemoryLLMClient:
    return FakeMemoryLLMClient()


@pytest.fixture()
def fake_conversation_reader() -> FakeConversationReader:
    return FakeConversationReader()


@pytest.fixture()
def fake_activity_reader() -> FakeActivityReader:
    return FakeActivityReader()


@pytest.fixture()
def runtime_context(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
    fake_activity_reader: FakeActivityReader,
) -> MemoryRuntimeContext:
    return MemoryRuntimeContext(
        settings=settings,
        memory_service=memory_service,
        graph_state_service=KnowledgeGraphStateService(
            settings=settings, session_factory=session_factory
        ),
        conversation_reader=fake_conversation_reader,
        activity_reader=fake_activity_reader,
        graph_registry_factory=default_registry_factory,
        openai_client=fake_llm,
        session_factory=session_factory,
        clock=SystemClock(),
        id_generator=SystemIdGenerator(),
        logger=logging.getLogger("test.graph"),
    )


@pytest.fixture()
def runner(runtime_context: MemoryRuntimeContext) -> LocalLangGraphRunner:
    return LocalLangGraphRunner(context=runtime_context)
