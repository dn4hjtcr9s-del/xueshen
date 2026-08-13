"""Access token 签发单元测试（方案 §4.4）：claims 完整性与 verifier 契约联调。"""

from __future__ import annotations

import time
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.auth_service.tokens import AccessTokenIssuer
from backend.settings import Settings


@pytest.fixture(scope="module")
def keypair(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str, str]:
    """运行时生成 RSA 2048 密钥对（方案 §11：测试密钥运行时生成）。"""
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
    tmp = tmp_path_factory.mktemp("auth_keys")
    private_file = tmp / "auth_private.pem"
    private_file.write_text(private_pem, encoding="utf-8")
    return private_pem, public_pem, str(private_file)


def _settings(public_pem: str, private_file: str) -> Settings:
    return Settings(
        app_env="test",
        auth_issuer="gewu-auth",
        auth_audience="memory-api",
        auth_public_key=public_pem,
        auth_private_key_file=private_file,
    )


def test_issued_token_claims_are_complete_and_verifiable(
    keypair: tuple[str, str, str],
) -> None:
    _private_pem, public_pem, private_file = keypair
    issuer = AccessTokenIssuer(_settings(public_pem, private_file))
    user_id = uuid4()
    token = issuer.issue(user_id=user_id)

    claims = jwt.decode(
        token,
        public_pem,
        algorithms=["RS256"],
        audience="memory-api",
        issuer="gewu-auth",
        options={"require": ["iss", "aud", "sub", "iat", "exp", "jti", "actor_type", "scopes"]},
    )
    assert claims["sub"] == str(user_id)
    assert claims["actor_type"] == "user"
    assert isinstance(claims["scopes"], list)
    assert set(claims["scopes"]) == {
        "memory:read",
        "memory:submit_evidence",
        "memory:correct",
        "memory:delete",
        "memory:restore",
        "memory:review",
        "memory:cancel",
        "memory:graph_state",
        "memory:context",
    }
    assert claims["exp"] - claims["iat"] == 300


def test_token_expiry_honours_lifetime_cap(keypair: tuple[str, str, str]) -> None:
    _private_pem, public_pem, private_file = keypair
    issuer = AccessTokenIssuer(_settings(public_pem, private_file))
    token = issuer.issue(user_id=uuid4(), lifetime_seconds=60)
    claims = jwt.decode(
        token, public_pem, algorithms=["RS256"], audience="memory-api", issuer="gewu-auth"
    )
    assert claims["exp"] - claims["iat"] == 60


def test_tokens_are_unique_via_jti(keypair: tuple[str, str, str]) -> None:
    _private_pem, public_pem, private_file = keypair
    issuer = AccessTokenIssuer(_settings(public_pem, private_file))
    user_id = uuid4()
    first = jwt.decode(
        issuer.issue(user_id=user_id),
        public_pem,
        algorithms=["RS256"],
        audience="memory-api",
        issuer="gewu-auth",
    )
    time.sleep(1)
    second = jwt.decode(
        issuer.issue(user_id=user_id),
        public_pem,
        algorithms=["RS256"],
        audience="memory-api",
        issuer="gewu-auth",
    )
    assert first["jti"] != second["jti"]


# ---------------------------------------------------------------------------
# AccessTokenVerifier.verify_sub 严格契约（复审 P3）：与 verifier 对齐
# ---------------------------------------------------------------------------


def _craft_token(
    private_pem: str,
    *,
    sub: str = "sub",
    actor_type: str = "user",
    scopes: list[str] | None = None,
    lifetime: int = 300,
) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": "gewu-auth",
            "aud": "memory-api",
            "sub": sub,
            "actor_type": actor_type,
            "scopes": scopes if scopes is not None else ["memory:read"],
            "iat": now,
            "exp": now + lifetime,
            "jti": str(uuid4()),
        },
        private_pem,
        algorithm="RS256",
    )


async def _verify(keypair: tuple[str, str, str], token: str) -> None:
    _private_pem, public_pem, private_file = keypair
    from backend.auth_service.tokens import AccessTokenVerifier

    await AccessTokenVerifier(_settings(public_pem, private_file)).verify_sub(token)


def test_verify_sub_accepts_valid_user_token(keypair: tuple[str, str, str]) -> None:
    import asyncio

    private_pem, public_pem, private_file = keypair
    from backend.auth_service.tokens import AccessTokenVerifier

    user_id = uuid4()
    token = _craft_token(private_pem, sub=str(user_id))
    result = asyncio.run(AccessTokenVerifier(_settings(public_pem, private_file)).verify_sub(token))
    assert result == user_id


def test_verify_sub_rejects_non_uuid_sub(keypair: tuple[str, str, str]) -> None:
    """复审 P3：合法签名但 sub 非 UUID（如 agent token）→ InvalidTokenError，不抛 ValueError。"""
    import asyncio

    private_pem, *_ = keypair
    token = _craft_token(private_pem, sub="conversation-agent-prod-01")
    with pytest.raises(jwt.InvalidTokenError):
        asyncio.run(_verify(keypair, token))


def test_verify_sub_rejects_missing_actor_type(keypair: tuple[str, str, str]) -> None:
    import asyncio

    private_pem, *_ = keypair
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": "gewu-auth",
            "aud": "memory-api",
            "sub": str(uuid4()),
            "scopes": ["memory:read"],
            "iat": now,
            "exp": now + 300,
            "jti": str(uuid4()),
        },
        private_pem,
        algorithm="RS256",
    )
    with pytest.raises(jwt.InvalidTokenError):
        asyncio.run(_verify(keypair, token))


def test_verify_sub_rejects_over_lifetime_cap(keypair: tuple[str, str, str]) -> None:
    import asyncio

    private_pem, *_ = keypair
    token = _craft_token(private_pem, sub=str(uuid4()), lifetime=301)
    with pytest.raises(jwt.InvalidTokenError):
        asyncio.run(_verify(keypair, token))
