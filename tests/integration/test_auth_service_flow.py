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


async def test_mapping_conflict_points_to_other_user_is_dead(
    auth_api_client: httpx.AsyncClient,
    auth_session_factory: async_sessionmaker[AsyncSession],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """评审 P1-6：映射已指向其他内部用户时，补偿事件转 dead，绝不静默成功。"""
    from uuid import uuid4

    client = auth_api_client
    resp = await client.post(
        "/api/v1/auth/register",
        json={"username": USERNAME, "password": PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["user"]["user_id"]

    # 人为制造冲突：(issuer, external_subject) 已指向另一个内部用户
    other_user = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO account_identity_mappings "
                    "(internal_user_id, issuer, external_subject) "
                    "VALUES (:other, :issuer, :sub)"
                ),
                {"other": other_user, "issuer": MAPPING_ISSUER, "sub": user_id},
            )

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
        assert row.scalar_one() == "dead"

    # 冲突行保持不变（未被覆盖）
    async with session_factory() as session:
        row = await session.execute(
            text(
                "SELECT internal_user_id FROM account_identity_mappings "
                "WHERE issuer = :i AND external_subject = :s"
            ),
            {"i": MAPPING_ISSUER, "s": user_id},
        )
        assert str(row.scalar_one()) == str(other_user)


async def test_login_rejected_when_mapping_points_to_other_user(
    auth_api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """评审 P1-6：登录兜底发现映射归属不一致时拒绝登录（fail-closed）。"""
    from uuid import uuid4

    client = auth_api_client
    resp = await client.post(
        "/api/v1/auth/register",
        json={"username": USERNAME, "password": PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    user_id = resp.json()["user"]["user_id"]

    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO account_identity_mappings "
                    "(internal_user_id, issuer, external_subject) "
                    "VALUES (:other, :issuer, :sub)"
                ),
                {"other": uuid4(), "issuer": MAPPING_ISSUER, "sub": user_id},
            )

    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": USERNAME, "password": PASSWORD},
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()["error"]
    assert body["code"] == "AUTH_MAPPING_PENDING"


async def test_login_rate_limit_account_bucket_shared_across_identifiers(
    auth_api_client: httpx.AsyncClient,
) -> None:
    """复审 P2-7：用户名与邮箱共用同一个账号限流桶（key=user_id）。"""
    client = auth_api_client
    resp = await client.post(
        "/api/v1/auth/register",
        json={
            "username": USERNAME,
            "email": f"{USERNAME}@example.com",
            "password": PASSWORD,
        },
    )
    assert resp.status_code == 201, resp.text

    identifiers = [USERNAME, f"{USERNAME}@example.com"] * 3  # 6 次尝试
    statuses = []
    for identifier in identifiers[:5]:
        resp = await client.post(
            "/api/v1/auth/login",
            json={"identifier": identifier, "password": "wrong-password-123"},
        )
        statuses.append(resp.status_code)
    assert statuses == [401] * 5

    # 第 6 次（无论用用户名还是邮箱）都应命中账号桶限流 429
    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": identifiers[5], "password": "wrong-password-123"},
    )
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["code"] == "RATE_LIMITED"


async def test_expired_family_cleanup_boundary(
    auth_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """复审 P2-5：过期恰好 29 天保留、31 天删除（不再额外加 30 天 TTL）。"""
    from datetime import UTC, datetime, timedelta
    from uuid import uuid4

    from backend.auth_service.session import delete_expired_families

    user_id = uuid4()
    async with auth_session_factory() as session:
        async with session.begin():
            await session.execute(
                text("INSERT INTO users (user_id, username, password_hash) VALUES (:u, :n, :h)"),
                {"u": user_id, "n": f"cleanup_{uuid4().hex[:8]}", "h": "hash"},
            )

    keep_family = uuid4()
    purge_family = uuid4()
    now = datetime.now(UTC)
    async with auth_session_factory() as session:
        async with session.begin():
            for family, expires in (
                (keep_family, now - timedelta(days=29)),
                (purge_family, now - timedelta(days=31)),
            ):
                await session.execute(
                    text(
                        "INSERT INTO refresh_tokens "
                        "(token_hash, user_id, family_id, expires_at) "
                        "VALUES (decode(md5(CAST(:family AS text) "
                        "|| CAST(:expires AS text)), 'hex'), "
                        ":user_id, :family, :expires)"
                    ),
                    {"family": family, "expires": expires, "user_id": user_id},
                )

    async with auth_session_factory() as session:
        async with session.begin():
            deleted = await delete_expired_families(session)
    assert deleted == 1

    async with auth_session_factory() as session:
        rows = await session.execute(
            text("SELECT family_id FROM refresh_tokens WHERE user_id = :u"),
            {"u": user_id},
        )
        remaining = {str(row[0]) for row in rows.all()}
    assert str(purge_family) not in remaining
    assert str(keep_family) in remaining


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
