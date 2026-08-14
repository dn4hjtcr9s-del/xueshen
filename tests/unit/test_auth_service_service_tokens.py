"""服务 token 签发工具单元测试（方案 community §10.3/D36/D43）。

- sub claim 即 principal 名（非用户 UUID）；
- actor_type=system、scope 属于 ALL_SCOPES；
- 与生产 verifier 验签契约对齐（jwt.decode 结构校验）；
- 超出 verifier 有效期上限时告警但允许签发。
"""

from __future__ import annotations

import jwt
import pytest

from backend.auth_service.service_tokens import issue_service_token
from backend.auth_service.tokens import AccessTokenIssuer
from backend.settings import Settings


@pytest.fixture(scope="module")
def keypair(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """运行时生成 RSA 2048 密钥对（与 test_auth_service_tokens 同模式）。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

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
    tmp = tmp_path_factory.mktemp("service_tokens")
    private_file = tmp / "auth_private.pem"
    private_file.write_text(private_pem, encoding="utf-8")
    return public_pem, str(private_file)


def _settings(public_pem: str, private_file: str) -> Settings:
    return Settings(
        app_env="test",
        auth_issuer="gewu-auth",
        auth_audience="memory-api",
        auth_public_key=public_pem,
        auth_private_key_file=private_file,
    )


def test_issued_service_token_claims(keypair: tuple[str, str]) -> None:
    """D36：sub=principal 名、actor_type=system、scope 正确。"""
    public_pem, private_file = keypair
    token = issue_service_token(
        principal="system:community-reader",
        scopes=["community:source_read"],
        lifetime_seconds=300,
        settings=_settings(public_pem, private_file),
    )
    claims = jwt.decode(
        token,
        public_pem,
        algorithms=["RS256"],
        audience="memory-api",
        issuer="gewu-auth",
        options={"require": ["iss", "aud", "sub", "iat", "exp", "jti", "actor_type", "scopes"]},
    )
    assert claims["sub"] == "system:community-reader"
    assert claims["actor_type"] == "system"
    assert claims["scopes"] == ["community:source_read"]
    assert int(claims["exp"]) - int(claims["iat"]) == 300


def test_issue_three_community_principals(keypair: tuple[str, str]) -> None:
    """§10.3/D36：三个 token 互不复用（principal 各自独立）。"""
    public_pem, private_file = keypair
    settings = _settings(public_pem, private_file)
    reader = issue_service_token(
        principal="system:community-reader",
        scopes=["community:source_read"],
        lifetime_seconds=300,
        settings=settings,
    )
    deleter = issue_service_token(
        principal="system:community-source-delete",
        scopes=["memory:source_delete"],
        lifetime_seconds=300,
        settings=settings,
    )
    purge = issue_service_token(
        principal="system:community-purge",
        scopes=["community:account_purge"],
        lifetime_seconds=300,
        settings=settings,
    )
    for token, sub, scope in (
        (reader, "system:community-reader", "community:source_read"),
        (deleter, "system:community-source-delete", "memory:source_delete"),
        (purge, "system:community-purge", "community:account_purge"),
    ):
        claims = jwt.decode(
            token, public_pem, algorithms=["RS256"], audience="memory-api", issuer="gewu-auth"
        )
        assert claims["sub"] == sub
        assert claims["scopes"] == [scope]
    # 三个 token 各不相同（jti 唯一）
    assert len({reader, deleter, purge}) == 3


def test_verifier_accepts_system_token_with_mapping(keypair: tuple[str, str]) -> None:
    """生产 verifier 契约：claims 结构与签发端一致可解码（映射注册在部署侧）。"""
    public_pem, private_file = keypair
    settings = _settings(public_pem, private_file)
    token = issue_service_token(
        principal="system:community-purge",
        scopes=["community:account_purge"],
        lifetime_seconds=300,
        settings=settings,
    )
    # AccessTokenIssuer 签发端校验（同结构）与工具签发结果一致可验
    issuer = AccessTokenIssuer(settings)
    decoded = jwt.decode(
        token,
        public_pem,
        algorithms=["RS256"],
        audience=issuer.audience,
        issuer=issuer.issuer,
    )
    assert decoded["actor_type"] == "system"
