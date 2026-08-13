"""集成测试基础设施：真实 PostgreSQL 容器 + 临时文件系统（§23.3）。

- 数据库使用 docker compose 的 PostgreSQL（settings 默认 127.0.0.1:55432/memory），
  与 ci-local.sh backend-integration 阶段一致；alembic upgrade head 幂等执行。
- 每个测试函数 TRUNCATE 全部用户数据表；图谱注册表（knowledge_graph_*）保留，
  其数据由 sync-knowledge-graph CLI 管理。
- Markdown 存储根使用 pytest tmp_path，测试间完全隔离。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
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
    "ops.account_deletion_ledger",
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


# ---------------------------------------------------------------------------
# 认证服务集成测试设施（方案 §11 / 附录 A.6 #20）：auth_test 独立库 + 运行时密钥
# ---------------------------------------------------------------------------

import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from backend.app import create_app  # noqa: E402
from backend.auth_service.database import AuthDatabase  # noqa: E402
from backend.auth_service.runtime import build_auth_runtime  # noqa: E402
from backend.memory.api.dependencies import ApiRuntime  # noqa: E402


def _admin_user() -> str:
    return os.environ.get("POSTGRES_ADMIN_USER", "postgres")


def _run_docker(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="session")
def auth_database_url() -> Iterator[str]:
    """auth_test 独立库：管理员创建、auth 角色所有（附录 A.6 #20）。"""
    if shutil.which("docker") is None:
        pytest.skip("需要 docker 创建隔离 auth 测试数据库")
    db_name = f"auth_test_{uuid4().hex[:8]}"
    base = "postgresql+psycopg://auth:auth@127.0.0.1:55432/"
    _run_docker("createdb", "-U", _admin_user(), "-O", "auth", db_name)
    url = base + db_name
    try:
        previous = os.environ.get("AUTH_DATABASE_URL")
        os.environ["AUTH_DATABASE_URL"] = url
        try:
            command.upgrade(AlembicConfig("auth_alembic.ini"), "head")
        finally:
            if previous is None:
                os.environ.pop("AUTH_DATABASE_URL", None)
            else:
                os.environ["AUTH_DATABASE_URL"] = previous
        yield url
    finally:
        _run_docker("dropdb", "-U", _admin_user(), db_name)


@pytest.fixture(scope="session")
def auth_test_settings(
    auth_database_url: str, tmp_path_factory: pytest.TempPathFactory
) -> Settings:
    """DEV_AUTH_ENABLED=false + 运行时生成的 RSA 2048 密钥对（方案 §11）。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    keys_dir = tmp_path_factory.mktemp("auth_integration_keys")
    private_file = keys_dir / "auth_private.pem"
    private_file.write_text(private_pem, encoding="utf-8")
    return Settings(
        app_env="test",
        dev_auth_enabled=False,
        dev_auth_allow_scope_override=False,
        auth_issuer="gewu-auth",
        auth_audience="memory-api",
        auth_database_url=auth_database_url,
        auth_private_key_file=str(private_file),
        auth_public_key=public_pem,
    )


@pytest.fixture()
async def auth_session_factory(
    auth_test_settings: Settings,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db = AuthDatabase(auth_test_settings)
    yield db.session_factory
    await db.close()


@pytest.fixture()
async def auth_api_client(
    auth_test_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    runner: LocalLangGraphRunner,
) -> AsyncIterator[httpx.AsyncClient]:
    """进程内 app：memory runtime（共享 memory 库）+ auth runtime（auth_test 库）。"""
    runtime = ApiRuntime(
        settings=auth_test_settings,
        session_factory=session_factory,
        memory_service=memory_service,
        runner=runner,
        gateway_worker=None,  # type: ignore[arg-type]  # 认证测试只走读路径
    )
    auth_runtime = build_auth_runtime(auth_test_settings, memory_session_factory=session_factory)
    app = create_app(auth_test_settings, runtime=runtime, auth_runtime=auth_runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        await auth_runtime.database.close()
