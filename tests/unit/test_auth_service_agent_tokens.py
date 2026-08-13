"""Agent 委托 token 单元测试（方案 §8 / §18.4）：claims 契约与 verifier 解析。"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.auth.verifier import ProductionJwtAuthAdapter
from backend.auth_service.agent_tokens import issue_agent_token
from backend.settings import Settings


@pytest.fixture(scope="module")
def keypair(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """运行时生成 RSA 2048 密钥对（方案 §11）。"""
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
    tmp = tmp_path_factory.mktemp("agent_keys")
    private_file = tmp / "auth_private.pem"
    private_file.write_text(private_pem, encoding="utf-8")
    return public_pem, str(private_file)


@pytest.fixture()
def settings(keypair: tuple[str, str]) -> Settings:
    public_pem, private_file = keypair
    return Settings(
        auth_issuer="gewu-auth",
        auth_audience="memory-api",
        auth_public_key=public_pem,
        auth_private_key_file=private_file,
    )


def test_agent_token_claims_contract(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("backend.auth_service.agent_tokens.get_settings", lambda: settings)
    token = issue_agent_token(
        agent_subject="conversation-agent-prod-01",
        delegated_sub=str(uuid4()),
        actor_type="conversation_agent",
        requested_scopes=["memory:read", "memory:context"],
    )
    claims = jwt.decode(
        token,
        settings.auth_public_key,
        algorithms=["RS256"],
        audience="memory-api",
        issuer="gewu-auth",
        options={"require": ["iss", "aud", "sub", "iat", "exp", "jti", "actor_type", "scopes"]},
    )
    assert claims["sub"] == "conversation-agent-prod-01"
    assert claims["actor_type"] == "conversation_agent"
    assert claims["delegated_sub"].startswith("00000000-0000") or claims["delegated_sub"]
    assert set(claims["scopes"]) == {"memory:read", "memory:context"}
    assert claims["exp"] - claims["iat"] == 300


def test_agent_token_scope_overreach_raises(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("backend.auth_service.agent_tokens.get_settings", lambda: settings)
    with pytest.raises(ValueError, match="Agent scope 越界"):
        issue_agent_token(
            agent_subject="activity-agent-prod-01",
            delegated_sub=str(uuid4()),
            actor_type="activity_agent",
            requested_scopes=["memory:delete"],
        )


def test_agent_token_resolves_delegated_user_in_verifier(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """verifier 将 agent 身份解析回委托用户 user_id（§3.1 第 3 条）。"""
    monkeypatch.setattr("backend.auth_service.agent_tokens.get_settings", lambda: settings)
    delegated = uuid4()
    token = issue_agent_token(
        agent_subject="activity-agent-prod-01",
        delegated_sub=str(delegated),
        actor_type="activity_agent",
        requested_scopes=["memory:read"],
    )

    class Resolver:
        async def resolve(self, *, issuer: str, external_subject: str) -> UUID | None:
            assert external_subject == str(delegated)
            return delegated

    adapter = ProductionJwtAuthAdapter(settings=settings, identity_resolver=Resolver())
    ctx = asyncio.run(
        adapter.authenticate({"authorization": f"Bearer {token}"}, client_host=None)
    )
    assert ctx.user_id == delegated
    assert ctx.actor_type == "activity_agent"
    assert ctx.actor_principal == "activity-agent-prod-01"
    assert ctx.scopes == frozenset({"memory:read"})
