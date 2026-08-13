"""认证服务 ↔ memory verifier 契约联调集成测试（方案 §6 步骤 6 / §11 第 2 条）。

真实 auth 测试库 + 真实 RSA 密钥（运行时生成）+ DEV_AUTH_ENABLED=false：
注册 → 登录 → 真 token 调 memory API 200；无 token / 篡改 token → 401。
身份映射由登录兜底即时补建（测试不依赖补偿消费任务常驻）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from uuid import uuid4

import httpx
import pytest
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command as alembic_command
from backend.app import create_app
from backend.auth_service.runtime import build_auth_runtime
from backend.memory.api.dependencies import ApiRuntime
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.services.memory_service import MemoryService
from backend.settings import Settings

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="需要 docker 创建隔离 auth 测试数据库",
)

TEST_USERNAME = "verifierlink"
TEST_PASSWORD = "verifier-pass-123"

#: 测试注册产生的 user_id，teardown 时清理共享 memory 库映射行
_registered_user_id: str | None = None


def _admin_user() -> str:
    return os.environ.get("POSTGRES_ADMIN_USER", "postgres")


def _run_docker(*args: str) -> None:
    subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture(scope="module")
def auth_database_url() -> Iterator[str]:
    """auth_test 独立库：管理员创建、auth 角色所有（附录 A.6 #20）。"""
    db_name = f"auth_test_{uuid4().hex[:8]}"
    base = "postgresql+psycopg://auth:auth@127.0.0.1:55432/"
    _run_docker("createdb", "-U", _admin_user(), "-O", "auth", db_name)
    url = base + db_name
    try:
        previous = os.environ.get("AUTH_DATABASE_URL")
        os.environ["AUTH_DATABASE_URL"] = url
        try:
            alembic_command.upgrade(AlembicConfig("auth_alembic.ini"), "head")
        finally:
            if previous is None:
                os.environ.pop("AUTH_DATABASE_URL", None)
            else:
                os.environ["AUTH_DATABASE_URL"] = previous
        yield url
    finally:
        _run_docker("dropdb", "-U", _admin_user(), db_name)


@pytest.fixture(scope="module")
def auth_settings(auth_database_url: str, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    """DEV_AUTH_ENABLED=false + 运行时生成的 RSA 2048 密钥对（方案 §11）。"""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    keys_dir = tmp_path_factory.mktemp("verifier_link_keys")
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
async def api_client(
    auth_settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    runner: LocalLangGraphRunner,
) -> AsyncIterator[httpx.AsyncClient]:
    """进程内 app：memory runtime（共享 dev memory 库）+ auth runtime（auth_test 库）。"""
    global _registered_user_id
    _registered_user_id = None
    runtime = ApiRuntime(
        settings=auth_settings,
        session_factory=session_factory,
        memory_service=memory_service,
        runner=runner,
        gateway_worker=None,  # type: ignore[arg-type]  # 只读路径无需 gateway worker
    )
    auth_runtime = build_auth_runtime(auth_settings, memory_session_factory=session_factory)
    app = create_app(auth_settings, runtime=runtime, auth_runtime=auth_runtime)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        # 清理本次注册写入共享 memory 库的映射行
        if _registered_user_id is not None:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            "DELETE FROM account_identity_mappings "
                            "WHERE issuer = 'gewu-auth' AND external_subject = :sub"
                        ),
                        {"sub": _registered_user_id},
                    )
        await auth_runtime.database.close()


async def test_real_token_accesses_memory_api(api_client: httpx.AsyncClient) -> None:
    """注册 → 登录 → 真 token 调 memory API 200；无 token / 篡改 → 401。"""
    global _registered_user_id

    # 1. 注册（真实 auth_test 库）
    resp = await api_client.post(
        "/api/v1/auth/register",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    _registered_user_id = resp.json()["user"]["user_id"]

    # 2. 登录 → access token（登录兜底即时补建映射）
    resp = await api_client.post(
        "/api/v1/auth/login",
        json={"identifier": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]

    # 3. 真 token 调 memory API → 200（verifier 契约联调，DEV_AUTH_ENABLED=false）
    resp = await api_client.get(
        "/api/v1/memory/index", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200, resp.text

    # 4. 无 token → 401
    resp = await api_client.get("/api/v1/memory/index")
    assert resp.status_code == 401, resp.text

    # 5. 篡改 token → 401
    resp = await api_client.get(
        "/api/v1/memory/index", headers={"Authorization": f"Bearer {token}x"}
    )
    assert resp.status_code == 401, resp.text
