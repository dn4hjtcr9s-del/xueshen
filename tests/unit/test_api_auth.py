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
    settings = Settings(
        app_env="production",
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
# DevelopmentAuthAdapter 细节（§18.1）
# ---------------------------------------------------------------------------


async def test_dev_adapter_forces_user_actor_and_default_scopes(
    tmp_path: Any, monkeypatch: Any
) -> None:
    adapter = DevelopmentAuthAdapter(_settings(tmp_path))
    context = await adapter.authenticate({"x-dev-user-id": str(USER_ID)})
    assert context.actor_type == "user"
    assert context.has_scope("memory:read")
    assert not context.has_scope("memory:maintenance")
    assert not context.has_scope("memory:break_glass")


async def test_dev_adapter_rejected_in_production(tmp_path: Any, monkeypatch: Any) -> None:
    settings = Settings(
        app_env="production",
        dev_auth_enabled=False,
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
        memory_storage_root=str(tmp_path / "storage"),
    )
    app, *_ = build_test_app(settings, monkeypatch=monkeypatch)
    client = TestClient(app)
    response = client.options(
        "/api/v1/memory/learner",
        headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in response.headers
