"""认证与权限矩阵单元测试（§18.1–§18.3 / §23.5）。

覆盖：401/403、dev auth 规则、生产拒绝 dev auth、admin 默认不能读正文、
生产 JWT 适配器（iss/aud/exp/jti/生命周期/身份映射）。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from backend.auth.verifier import (
    AuthError,
    DevelopmentAuthAdapter,
    ProductionJwtAuthAdapter,
)
from backend.settings import Settings
from tests.unit.api_fakes import build_test_app

USER_ID = uuid4()
OTHER_USER_ID = uuid4()


def _settings(tmp_path: Any, **overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "app_env": "development",
        "dev_auth_enabled": True,
        "memory_storage_root": str(tmp_path / "storage"),
    }
    base.update(overrides)
    return Settings(**base)


def _client(tmp_path: Any, monkeypatch: Any, **overrides: Any) -> TestClient:
    app, *_ = build_test_app(_settings(tmp_path, **overrides), monkeypatch=monkeypatch)
    return TestClient(app)


def _auth(user_id: UUID = USER_ID, **extra: str) -> dict[str, str]:
    return {"X-Dev-User-Id": str(user_id), **extra}


# ---------------------------------------------------------------------------
# 401 / 403
# ---------------------------------------------------------------------------


def test_missing_auth_returns_401(tmp_path: Any, monkeypatch: Any) -> None:
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/v1/memory/learner")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"
    assert response.json()["error"]["trace_id"]


def test_invalid_dev_user_id_returns_401(tmp_path: Any, monkeypatch: Any) -> None:
    client = _client(tmp_path, monkeypatch)
    response = client.get("/api/v1/memory/learner", headers={"X-Dev-User-Id": "not-a-uuid"})
    assert response.status_code == 401


def test_missing_scope_returns_403(tmp_path: Any, monkeypatch: Any) -> None:
    client = _client(tmp_path, monkeypatch, dev_auth_allow_scope_override=True)
    response = client.get(
        "/api/v1/memory/learner",
        headers=_auth(**{"X-Dev-Scopes": "memory:cancel"}),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "AUTH_FORBIDDEN"


def test_scope_override_ignored_when_disabled(tmp_path: Any, monkeypatch: Any) -> None:
    """未开启 DEV_AUTH_ALLOW_SCOPE_OVERRIDE 时忽略提权头（§18.1）。"""
    client = _client(tmp_path, monkeypatch, dev_auth_allow_scope_override=False)
    response = client.get(
        "/api/v1/memory/learner",
        headers=_auth(**{"X-Dev-Actor-Type": "admin", "X-Dev-Scopes": "memory:read"}),
    )
    # actor_type 被强制为 user：走到业务逻辑（learner 不存在 → 404），而不是 admin 403
    assert response.status_code == 404


def test_unknown_scope_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    client = _client(tmp_path, monkeypatch, dev_auth_allow_scope_override=True)
    response = client.get(
        "/api/v1/memory/learner",
        headers=_auth(**{"X-Dev-Scopes": "memory:read god:mode"}),
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 权限矩阵（§18.3）
# ---------------------------------------------------------------------------


def test_admin_cannot_read_body_by_default(tmp_path: Any, monkeypatch: Any) -> None:
    """admin 普通 token 即使带全部 scope 也不能读正文（§18.3 矩阵）。"""
    client = _client(tmp_path, monkeypatch, dev_auth_allow_scope_override=True)
    response = client.get(
        "/api/v1/memory/learner",
        headers=_auth(**{"X-Dev-Actor-Type": "admin", "X-Dev-Scopes": "memory:read"}),
    )
    assert response.status_code == 403


def test_conversation_agent_cannot_read_memory(tmp_path: Any, monkeypatch: Any) -> None:
    client = _client(tmp_path, monkeypatch, dev_auth_allow_scope_override=True)
    response = client.get(
        "/api/v1/memory/learner",
        headers=_auth(**{"X-Dev-Actor-Type": "conversation_agent", "X-Dev-Scopes": "memory:read"}),
    )
    assert response.status_code == 403


def test_user_cannot_call_internal_purge(tmp_path: Any, monkeypatch: Any) -> None:
    client = _client(tmp_path, monkeypatch, dev_auth_allow_scope_override=True)
    response = client.post(
        "/api/v1/internal/account-memory/purge",
        headers=_auth(**{"X-Dev-Scopes": "memory:read memory:maintenance"}),
        json={
            "account_deletion_id": str(uuid4()),
            "issuer": "https://accounts.example",
            "external_subject": "sub-1",
            "requested_at": datetime.now(UTC).isoformat(),
            "reason": "用户注销",
        },
    )
    # user actor 不在白名单；scope 检查在 actor 之后
    assert response.status_code == 403


def test_system_actor_without_maintenance_scope_cannot_purge(
    tmp_path: Any, monkeypatch: Any
) -> None:
    client = _client(tmp_path, monkeypatch, dev_auth_allow_scope_override=True)
    response = client.post(
        "/api/v1/internal/account-memory/purge",
        headers=_auth(**{"X-Dev-Actor-Type": "system", "X-Dev-Scopes": "memory:read"}),
        json={
            "account_deletion_id": str(uuid4()),
            "issuer": "https://accounts.example",
            "external_subject": "sub-1",
            "requested_at": datetime.now(UTC).isoformat(),
            "reason": "用户注销",
        },
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# 生产环境约束（§18.1）
# ---------------------------------------------------------------------------


def test_production_rejects_dev_auth_enabled() -> None:
    with pytest.raises(ValueError, match="DEV_AUTH"):
        Settings(app_env="production", dev_auth_enabled=True)


def test_production_rejects_dev_scope_override() -> None:
    with pytest.raises(ValueError, match="DEV_AUTH"):
        Settings(app_env="production", dev_auth_allow_scope_override=True)


def test_production_readiness_fails_without_auth_config(tmp_path: Any, monkeypatch: Any) -> None:
    settings = Settings(
        app_env="production",
        dev_auth_enabled=False,
        # §6.3：密钥/auth 库缺失已由启动校验拦截（评审 #14）；readiness 的
        # production_auth_not_configured 现覆盖 issuer 缺失场景
        auth_private_key_file=str(tmp_path / "auth_private.pem"),
        auth_public_key="-----BEGIN PUBLIC KEY-----\ndummy\n-----END PUBLIC KEY-----",
        auth_database_url="postgresql+psycopg://auth:auth@db:5432/auth",
        memory_storage_root=str(tmp_path / "storage"),
    )
    app, *_ = build_test_app(settings, monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert "production_auth_not_configured" in response.json()["failures"]


# ---------------------------------------------------------------------------
# ProductionJwtAuthAdapter（§18.1）
# ---------------------------------------------------------------------------


def _rsa_keys() -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private_key.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    return private_pem, public_pem


class _StaticResolver:
    def __init__(self, user_id: UUID | None) -> None:
        self.user_id = user_id

    async def resolve(self, *, issuer: str, external_subject: str) -> UUID | None:
        return self.user_id


def _make_token(private_pem: str, **claims: Any) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "iss": "https://auth.example",
        "aud": "memory-api",
        "sub": "external-subject-1",
        "actor_type": "user",
        "scopes": ["memory:read"],
        "iat": now,
        "exp": now + 240,
        "jti": uuid4().hex,
    }
    payload.update(claims)
    return jwt.encode(payload, private_pem, algorithm="RS256")


def _prod_settings(tmp_path: Any, public_pem: str) -> Settings:
    return Settings(
        app_env="production",
        dev_auth_enabled=False,
        auth_issuer="https://auth.example",
        auth_public_key=public_pem,
        # §6.3 生产配置校验要求（评审 #14）
        auth_private_key_file=str(tmp_path / "auth_private.pem"),
        auth_database_url="postgresql+psycopg://auth:auth@db:5432/auth",
        memory_storage_root=str(tmp_path / "storage"),
    )


async def test_prod_jwt_valid_token_resolves_identity(tmp_path: Any, monkeypatch: Any) -> None:
    private_pem, public_pem = _rsa_keys()
    adapter = ProductionJwtAuthAdapter(
        settings=_prod_settings(tmp_path, public_pem),
        identity_resolver=_StaticResolver(USER_ID),
    )
    token = _make_token(private_pem)
    context = await adapter.authenticate({"authorization": f"Bearer {token}"})
    assert context.user_id == USER_ID
    assert context.actor_type == "user"
    assert context.has_scope("memory:read")
    assert context.issuer == "https://auth.example"
    assert context.external_subject == "external-subject-1"


async def test_prod_jwt_wrong_audience_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    private_pem, public_pem = _rsa_keys()
    adapter = ProductionJwtAuthAdapter(
        settings=_prod_settings(tmp_path, public_pem),
        identity_resolver=_StaticResolver(USER_ID),
    )
    token = _make_token(private_pem, aud="other-api")
    with pytest.raises(AuthError) as exc_info:
        await adapter.authenticate({"authorization": f"Bearer {token}"})
    assert exc_info.value.code == "AUTH_REQUIRED"


async def test_prod_jwt_expired_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    private_pem, public_pem = _rsa_keys()
    adapter = ProductionJwtAuthAdapter(
        settings=_prod_settings(tmp_path, public_pem),
        identity_resolver=_StaticResolver(USER_ID),
    )
    now = int(time.time())
    token = _make_token(private_pem, iat=now - 600, exp=now - 300)
    with pytest.raises(AuthError):
        await adapter.authenticate({"authorization": f"Bearer {token}"})


async def test_prod_jwt_lifetime_over_5_minutes_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    private_pem, public_pem = _rsa_keys()
    adapter = ProductionJwtAuthAdapter(
        settings=_prod_settings(tmp_path, public_pem),
        identity_resolver=_StaticResolver(USER_ID),
    )
    now = int(time.time())
    token = _make_token(private_pem, iat=now, exp=now + 600)
    with pytest.raises(AuthError, match="5 分钟"):
        await adapter.authenticate({"authorization": f"Bearer {token}"})


async def test_prod_jwt_missing_jti_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    private_pem, public_pem = _rsa_keys()
    adapter = ProductionJwtAuthAdapter(
        settings=_prod_settings(tmp_path, public_pem),
        identity_resolver=_StaticResolver(USER_ID),
    )
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "https://auth.example",
            "aud": "memory-api",
            "sub": "s1",
            "iat": now,
            "exp": now + 60,
        },
        private_pem,
        algorithm="RS256",
    )
    with pytest.raises(AuthError):
        await adapter.authenticate({"authorization": f"Bearer {token}"})


async def test_prod_jwt_unknown_identity_mapping_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    private_pem, public_pem = _rsa_keys()
    adapter = ProductionJwtAuthAdapter(
        settings=_prod_settings(tmp_path, public_pem),
        identity_resolver=_StaticResolver(None),
    )
    token = _make_token(private_pem)
    with pytest.raises(AuthError, match="身份映射不存在"):
        await adapter.authenticate({"authorization": f"Bearer {token}"})


async def test_prod_jwt_missing_key_material_rejected(tmp_path: Any, monkeypatch: Any) -> None:
    # §6.3：生产缺公钥材料直接启动失败；adapter 的"未配置"拒绝路径改由
    # test/staging 环境验证（生产 Settings 已无法构造出缺失公钥的状态）
    settings = Settings(
        app_env="test",
        dev_auth_enabled=False,
        auth_issuer="https://auth.example",
        memory_storage_root=str(tmp_path / "storage"),
    )
    adapter = ProductionJwtAuthAdapter(
        settings=settings, identity_resolver=_StaticResolver(USER_ID)
    )
    with pytest.raises(AuthError, match="未配置"):
        await adapter.authenticate({"authorization": "Bearer whatever"})


# ---------------------------------------------------------------------------
# Agent 委托契约（§18.4，评审 #15）
# ---------------------------------------------------------------------------


class _MappingResolver:
    """按 external_subject 查表的身份映射 fake。"""

    def __init__(self, mapping: dict[str, UUID]) -> None:
        self.mapping = mapping

    async def resolve(self, *, issuer: str, external_subject: str) -> UUID | None:
        return self.mapping.get(external_subject)


def _agent_token(private_pem: str, **claims: Any) -> str:
    defaults: dict[str, Any] = {
        "sub": "svc-conversation-agent",
        "actor_type": "conversation_agent",
        "scopes": ["memory:read", "memory:context"],
        "delegated_sub": "external-subject-1",
    }
    defaults.update(claims)
    return _make_token(private_pem, **defaults)


async def test_agent_token_with_delegated_sub_resolves_delegated_user(tmp_path: Any) -> None:
    private_pem, public_pem = _rsa_keys()
    adapter = ProductionJwtAuthAdapter(
        settings=_prod_settings(tmp_path, public_pem),
        identity_resolver=_MappingResolver({"external-subject-1": USER_ID}),
    )
    context = await adapter.authenticate({"authorization": f"Bearer {_agent_token(private_pem)}"})
    assert context.user_id == USER_ID
    assert context.actor_type == "conversation_agent"
    assert context.external_subject == "external-subject-1"
    assert context.actor_principal == "svc-conversation-agent"
    assert context.has_scope("memory:read")


async def test_agent_token_missing_delegated_sub_rejected(tmp_path: Any) -> None:
    private_pem, public_pem = _rsa_keys()
    adapter = ProductionJwtAuthAdapter(
        settings=_prod_settings(tmp_path, public_pem),
        identity_resolver=_MappingResolver({"external-subject-1": USER_ID}),
    )
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "https://auth.example",
            "aud": "memory-api",
            "sub": "svc-activity-agent",
            "actor_type": "activity_agent",
            "scopes": ["memory:read"],
            "iat": now,
            "exp": now + 240,
            "jti": uuid4().hex,
        },
        private_pem,
        algorithm="RS256",
    )
    with pytest.raises(AuthError, match="delegated_sub") as exc_info:
        await adapter.authenticate({"authorization": f"Bearer {token}"})
    assert exc_info.value.code == "AUTH_REQUIRED"


async def test_agent_token_unresolvable_delegated_sub_rejected(tmp_path: Any) -> None:
    private_pem, public_pem = _rsa_keys()
    adapter = ProductionJwtAuthAdapter(
        settings=_prod_settings(tmp_path, public_pem),
        identity_resolver=_MappingResolver({}),
    )
    with pytest.raises(AuthError, match="身份映射不存在") as exc_info:
        await adapter.authenticate({"authorization": f"Bearer {_agent_token(private_pem)}"})
    assert exc_info.value.code == "AUTH_REQUIRED"


async def test_agent_token_scope_overreach_rejected(tmp_path: Any) -> None:
    """Agent 不得持有委托契约之外的 scope（§18.3 矩阵：删除/纠正均为否）。"""
    private_pem, public_pem = _rsa_keys()
    adapter = ProductionJwtAuthAdapter(
        settings=_prod_settings(tmp_path, public_pem),
        identity_resolver=_MappingResolver({"external-subject-1": USER_ID}),
    )
    token = _agent_token(private_pem, scopes=["memory:read", "memory:delete"])
    with pytest.raises(AuthError, match="越界") as exc_info:
        await adapter.authenticate({"authorization": f"Bearer {token}"})
    assert exc_info.value.code == "AUTH_FORBIDDEN"
    assert exc_info.value.forbidden is True


async def test_user_token_without_delegation_unaffected(tmp_path: Any) -> None:
    """回归：普通 user token 不需要 delegated_sub，actor_principal 为空。"""
    private_pem, public_pem = _rsa_keys()
    adapter = ProductionJwtAuthAdapter(
        settings=_prod_settings(tmp_path, public_pem),
        identity_resolver=_MappingResolver({"external-subject-1": USER_ID}),
    )
    context = await adapter.authenticate({"authorization": f"Bearer {_make_token(private_pem)}"})
    assert context.user_id == USER_ID
    assert context.actor_principal is None


# ---------------------------------------------------------------------------
# 严格 claims schema（评审二轮 #4）：缺失/错型/未知值整体拒绝，不静默修正
# ---------------------------------------------------------------------------


def _prod_adapter(tmp_path: Any, public_pem: str) -> ProductionJwtAuthAdapter:
    return ProductionJwtAuthAdapter(
        settings=_prod_settings(tmp_path, public_pem),
        identity_resolver=_MappingResolver({"external-subject-1": USER_ID}),
    )


async def _expect_rejected(
    tmp_path: Any, private_pem: str, public_pem: str, **claims: Any
) -> AuthError:
    adapter = _prod_adapter(tmp_path, public_pem)
    token = _make_token(private_pem, **claims)
    with pytest.raises(AuthError) as exc_info:
        await adapter.authenticate({"authorization": f"Bearer {token}"})
    return exc_info.value


async def test_missing_actor_type_rejected(tmp_path: Any) -> None:
    private_pem, public_pem = _rsa_keys()
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "https://auth.example",
            "aud": "memory-api",
            "sub": "external-subject-1",
            "scopes": ["memory:read"],
            "iat": now,
            "exp": now + 240,
            "jti": uuid4().hex,
        },
        private_pem,
        algorithm="RS256",
    )
    adapter = _prod_adapter(tmp_path, public_pem)
    with pytest.raises(AuthError) as exc_info:
        await adapter.authenticate({"authorization": f"Bearer {token}"})
    assert exc_info.value.code == "AUTH_REQUIRED"


async def test_missing_scopes_rejected(tmp_path: Any) -> None:
    private_pem, public_pem = _rsa_keys()
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "https://auth.example",
            "aud": "memory-api",
            "sub": "external-subject-1",
            "actor_type": "user",
            "iat": now,
            "exp": now + 240,
            "jti": uuid4().hex,
        },
        private_pem,
        algorithm="RS256",
    )
    adapter = _prod_adapter(tmp_path, public_pem)
    with pytest.raises(AuthError) as exc_info:
        await adapter.authenticate({"authorization": f"Bearer {token}"})
    assert exc_info.value.code == "AUTH_REQUIRED"


@pytest.mark.parametrize(
    "bad_scopes",
    ["memory:read", {"scope": "memory:read"}, ["memory:read", 42], None],
)
async def test_malformed_scopes_rejected(tmp_path: Any, bad_scopes: Any) -> None:
    """scopes 为字符串/对象/含非字符串/非数组时整体拒绝（不得按字符遍历或静默过滤）。"""
    private_pem, public_pem = _rsa_keys()
    exc = await _expect_rejected(tmp_path, private_pem, public_pem, scopes=bad_scopes)
    assert exc.code == "AUTH_REQUIRED"


async def test_unknown_scope_rejected_not_filtered(tmp_path: Any) -> None:
    """未知 scope 拒绝整个 token，而不是静默丢弃后继续。"""
    private_pem, public_pem = _rsa_keys()
    exc = await _expect_rejected(
        tmp_path, private_pem, public_pem, scopes=["memory:read", "god:mode"]
    )
    assert exc.code == "AUTH_REQUIRED"
    assert "未知 scope" in str(exc)


@pytest.mark.parametrize("actor", ["user", "system", "admin"])
async def test_non_agent_token_with_delegated_sub_rejected(tmp_path: Any, actor: str) -> None:
    """非 Agent token 携带 delegated_sub 一律拒绝（委托 claim 与 actor 必须匹配）。"""
    private_pem, public_pem = _rsa_keys()
    exc = await _expect_rejected(
        tmp_path, private_pem, public_pem, actor_type=actor, delegated_sub="external-subject-1"
    )
    assert exc.code == "AUTH_REQUIRED"
    assert "delegated_sub" in str(exc)


async def test_unknown_actor_type_rejected(tmp_path: Any) -> None:
    private_pem, public_pem = _rsa_keys()
    exc = await _expect_rejected(tmp_path, private_pem, public_pem, actor_type="superuser")
    assert exc.code == "AUTH_REQUIRED"


# ---------------------------------------------------------------------------
# DevelopmentAuthAdapter 细节（§18.1）
# ---------------------------------------------------------------------------


async def test_dev_adapter_forces_user_actor_and_default_scopes(
    tmp_path: Any, monkeypatch: Any
) -> None:
    adapter = DevelopmentAuthAdapter(_settings(tmp_path))
    context = await adapter.authenticate({"x-dev-user-id": str(USER_ID)}, client_host="127.0.0.1")
    assert context.actor_type == "user"
    assert context.has_scope("memory:read")
    assert not context.has_scope("memory:maintenance")
    assert not context.has_scope("memory:break_glass")


# 评审 #9：Dev Auth 来源限制（§18.1 只能从 loopback/Compose 内网进入）


@pytest.mark.parametrize(
    "client_host",
    ["127.0.0.1", "::1", "10.0.0.5", "172.18.0.2", "192.168.1.10", "testclient"],
)
async def test_dev_adapter_allows_loopback_and_private_sources(
    tmp_path: Any, client_host: str
) -> None:
    adapter = DevelopmentAuthAdapter(_settings(tmp_path))
    context = await adapter.authenticate({"x-dev-user-id": str(USER_ID)}, client_host=client_host)
    assert context.user_id == USER_ID


@pytest.mark.parametrize("client_host", [None, "8.8.8.8", "1.2.3.4", "not-an-ip"])
async def test_dev_adapter_rejects_external_or_unknown_sources(
    tmp_path: Any, client_host: Any
) -> None:
    adapter = DevelopmentAuthAdapter(_settings(tmp_path))
    with pytest.raises(AuthError, match="loopback") as exc_info:
        await adapter.authenticate({"x-dev-user-id": str(USER_ID)}, client_host=client_host)
    assert exc_info.value.code == "AUTH_FORBIDDEN"
    assert exc_info.value.forbidden is True


async def test_dev_adapter_rejected_in_production(tmp_path: Any, monkeypatch: Any) -> None:
    settings = Settings(
        app_env="production",
        dev_auth_enabled=False,
        auth_private_key_file=str(tmp_path / "auth_private.pem"),
        auth_public_key="-----BEGIN PUBLIC KEY-----\ndummy\n-----END PUBLIC KEY-----",
        auth_database_url="postgresql+psycopg://auth:auth@db:5432/auth",
        memory_storage_root=str(tmp_path / "storage"),
    )
    adapter = DevelopmentAuthAdapter(settings)
    with pytest.raises(AuthError):
        await adapter.authenticate({"x-dev-user-id": str(USER_ID)})


def test_trace_id_inherited_from_traceparent(tmp_path: Any, monkeypatch: Any) -> None:
    client = _client(tmp_path, monkeypatch)
    trace_id = "ab" * 16
    response = client.get(
        "/api/v1/memory/learner",
        headers={"traceparent": f"00-{trace_id}-{'cd' * 8}-01"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["trace_id"] == trace_id


def test_cors_allows_only_configured_origins(tmp_path: Any, monkeypatch: Any) -> None:
    settings = _settings(tmp_path, memory_allowed_origins=["https://app.example"])
    app, *_ = build_test_app(settings, monkeypatch=monkeypatch)
    client = TestClient(app)
    allowed = client.options(
        "/api/v1/memory/learner",
        headers={
            "Origin": "https://app.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert allowed.headers.get("access-control-allow-origin") == "https://app.example"
    rejected = client.options(
        "/api/v1/memory/learner",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in rejected.headers


def test_cors_production_default_closed(tmp_path: Any, monkeypatch: Any) -> None:
    settings = Settings(
        app_env="production",
        dev_auth_enabled=False,
        auth_private_key_file=str(tmp_path / "auth_private.pem"),
        auth_public_key="-----BEGIN PUBLIC KEY-----\ndummy\n-----END PUBLIC KEY-----",
        auth_database_url="postgresql+psycopg://auth:auth@db:5432/auth",
        memory_storage_root=str(tmp_path / "storage"),
    )
    app, *_ = build_test_app(settings, monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.options(
        "/api/v1/memory/learner",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in response.headers
