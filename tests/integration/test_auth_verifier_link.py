"""认证服务 ↔ memory verifier 契约联调集成测试（方案 §6 步骤 6 / §11 第 2 条）。

真实 auth 测试库 + 真实 RSA 密钥（运行时生成）+ DEV_AUTH_ENABLED=false：
注册 → 登录 → 真 token 调 memory API 200；无 token / 篡改 token → 401。
身份映射由登录兜底即时补建（测试不依赖补偿消费任务常驻）。
"""

from __future__ import annotations

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.settings import Settings

pytestmark = pytest.mark.skipif(
    __import__("shutil").which("docker") is None,
    reason="需要 docker 创建隔离 auth 测试数据库",
)

TEST_USERNAME = "verifierlink"
TEST_PASSWORD = "verifier-pass-123"

#: 测试注册产生的 user_id，teardown 时清理共享 memory 库映射行
_registered_user_id: str | None = None


@pytest.fixture()
async def client(
    auth_api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> httpx.AsyncClient:
    global _registered_user_id
    _registered_user_id = None
    yield auth_api_client
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


async def test_real_token_accesses_memory_api(client: httpx.AsyncClient) -> None:
    """注册 → 登录 → 真 token 调 memory API 200；无 token / 篡改 → 401。"""
    global _registered_user_id

    # 1. 注册（真实 auth_test 库）
    resp = await client.post(
        "/api/v1/auth/register",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 201, resp.text
    _registered_user_id = resp.json()["user"]["user_id"]

    # 2. 登录 → access token（登录兜底即时补建映射）
    resp = await client.post(
        "/api/v1/auth/login",
        json={"identifier": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    token = resp.json()["access_token"]

    # 3. 真 token 调 memory API → 200（verifier 契约联调，DEV_AUTH_ENABLED=false）
    resp = await client.get("/api/v1/memory/index", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text

    # 4. 无 token → 401
    resp = await client.get("/api/v1/memory/index")
    assert resp.status_code == 401, resp.text

    # 5. 篡改 token → 401
    resp = await client.get("/api/v1/memory/index", headers={"Authorization": f"Bearer {token}x"})
    assert resp.status_code == 401, resp.text


async def test_expired_token_rejected(
    client: httpx.AsyncClient, auth_test_settings: Settings
) -> None:
    """过期 token → 401（方案 §11 第 3 条）。"""
    from uuid import uuid4

    from backend.auth_service.tokens import AccessTokenIssuer

    issuer = AccessTokenIssuer(auth_test_settings)
    expired = issuer.issue(user_id=uuid4(), lifetime_seconds=-60)
    resp = await client.get("/api/v1/memory/index", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401, resp.text
