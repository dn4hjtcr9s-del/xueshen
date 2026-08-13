"""认证服务会话管理集成测试（方案 §11 第 1/4/5 条）。

- 注册 → users + 补偿事件同事务落库；补偿消费任务 tick 后 mapping 建立。
- refresh 轮换；重放 → 整族撤销。
- logout 幂等；logout 后 refresh 401。
- auth 库异常 → readiness 503（§6.3）。
"""

from __future__ import annotations

import shutil

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.auth_service.mapping_consumer import IdentityMappingConsumer
from backend.settings import Settings

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="需要 docker 创建隔离 auth 测试数据库",
)

USERNAME = "sessionflow"
PASSWORD = "session-pass-123"
MAPPING_ISSUER = "gewu-auth"


@pytest.fixture(autouse=True)
async def _clean_auth_db(auth_session_factory: async_sessionmaker[AsyncSession]) -> None:
    """每个测试前清空 auth_test 库（users CASCADE 级联 refresh_tokens / outbox）。"""
    async with auth_session_factory() as session:
        async with session.begin():
            await session.execute(text("TRUNCATE users CASCADE"))


async def _register_and_login(
    client: httpx.AsyncClient, username: str
) -> tuple[str, dict[str, str]]:
    """注册 + 登录；返回 (user_id, cookies)。"""
    resp = await client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["user"]["user_id"]
    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": username, "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return user_id, dict(client.cookies)


async def test_register_persists_user_and_outbox_then_consumer_builds_mapping(
    auth_api_client: httpx.AsyncClient,
    auth_session_factory: async_sessionmaker[AsyncSession],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """注册 → users、outbox 事件落库；补偿任务消费后 mapping 建立（§11 第 1 条）。"""
    client = auth_api_client
    resp = await client.post(
        "/api/v1/auth/register",
        json={"username": USERNAME, "password": PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["user"]["user_id"]

    # users + outbox 同事务已提交（登录前不自动补建映射）
    async with auth_session_factory() as session:
        row = await session.execute(
            text("SELECT status, attempts FROM identity_mapping_outbox WHERE user_id = :u"),
            {"u": user_id},
        )
        outbox = row.first()
        assert outbox is not None and outbox.status == "pending"

    async with session_factory() as session:
        mapping = await session.execute(
            text(
                "SELECT 1 FROM account_identity_mappings "
                "WHERE issuer = :i AND external_subject = :s"
            ),
            {"i": MAPPING_ISSUER, "s": user_id},
        )
        assert mapping.first() is None, "登录前不应存在映射"

    # 补偿消费任务 tick → outbox done + mapping 建立
    consumer = IdentityMappingConsumer(
        auth_session_factory=auth_session_factory,
        memory_session_factory=session_factory,
        poll_interval_seconds=0.01,
    )
    processed = await consumer.tick()
    assert processed == 1

    async with auth_session_factory() as session:
        row = await session.execute(
            text("SELECT status FROM identity_mapping_outbox WHERE user_id = :u"),
            {"u": user_id},
        )
        assert row.scalar_one() == "done"

    async with session_factory() as session:
        mapping = await session.execute(
            text(
                "SELECT internal_user_id FROM account_identity_mappings "
                "WHERE issuer = :i AND external_subject = :s"
            ),
            {"i": MAPPING_ISSUER, "s": user_id},
        )
        assert str(mapping.scalar_one()) == user_id


async def test_refresh_rotation_and_replay_revokes_family(
    auth_api_client: httpx.AsyncClient,
) -> None:
    """refresh 轮换；重放旧 token → 整族撤销（§11 第 4 条）。"""
    client = auth_api_client
    _, cookies = await _register_and_login(client, USERNAME)
    first = cookies["gewu_refresh_token"]

    resp = await client.post("/api/v1/auth/refresh")
    assert resp.status_code == 200, resp.text
    second = client.cookies["gewu_refresh_token"]
    assert second != first, "refresh 必须轮换 token"

    # 重放旧 token → 401 且整族撤销（新 token 也失效）
    old_client = httpx.AsyncClient(
        transport=client._transport,
        base_url="http://test",  # type: ignore[attr-defined]
    )
    old_client.cookies.set("gewu_refresh_token", first)
    resp = await old_client.post("/api/v1/auth/refresh")
    assert resp.status_code == 401, resp.text

    resp = await client.post("/api/v1/auth/refresh")
    assert resp.status_code == 401, resp.text


async def test_logout_idempotent_and_revokes_session(
    auth_api_client: httpx.AsyncClient,
) -> None:
    """logout 幂等；logout 后 refresh → 401（§11 第 5 条）。"""
    client = auth_api_client
    _, _ = await _register_and_login(client, USERNAME)

    resp = await client.post("/api/v1/auth/logout")
    assert resp.status_code == 204
    assert "gewu_refresh_token" not in client.cookies

    resp = await client.post("/api/v1/auth/logout")
    assert resp.status_code == 204, "logout 必须幂等"

    resp = await client.post("/api/v1/auth/refresh")
    assert resp.status_code == 401, resp.text


async def test_readiness_503_when_auth_db_unavailable(
    auth_test_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    tmp_path: object,
) -> None:
    """auth 库异常 → readiness 503（§6.3 / §11 第 7 条）。"""
    from backend.app import create_app
    from backend.auth_service.database import AuthDatabase
    from backend.auth_service.runtime import AuthRuntime
    from backend.memory.api.dependencies import ApiRuntime
    from backend.memory.persistence.database import create_engine, create_session_factory
    from backend.memory.services.memory_service import MemoryService
    from backend.memory.storage.local_markdown import LocalMarkdownStore

    memory_settings = Settings(app_env="test", memory_storage_root=str(tmp_path) + "/storage")
    engine = create_engine(memory_settings)
    factory = create_session_factory(engine)
    store = LocalMarkdownStore(memory_settings.memory_storage_root)
    memory_service = MemoryService(settings=memory_settings, session_factory=factory, store=store)
    runtime = ApiRuntime(
        settings=memory_settings,
        session_factory=factory,
        memory_service=memory_service,
        runner=None,  # type: ignore[arg-type]
        gateway_worker=None,  # type: ignore[arg-type]
    )
    # auth 指向不可达端口
    broken_settings = auth_test_settings.model_copy(
        update={"auth_database_url": "postgresql+psycopg://auth:auth@127.0.0.1:59999/auth"}
    )
    broken_auth = AuthDatabase(broken_settings)
    auth_runtime = AuthRuntime(
        settings=broken_settings,
        database=broken_auth,
        session_factory=broken_auth.session_factory,
        issuer=None,  # type: ignore[arg-type]
        verifier=None,  # type: ignore[arg-type]
    )
    app = create_app(memory_settings, runtime=runtime, auth_runtime=auth_runtime)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health/ready")
    assert resp.status_code == 503
    assert "auth_database_unavailable" in resp.json()["failures"]
    await engine.dispose()
    await broken_auth.close()
