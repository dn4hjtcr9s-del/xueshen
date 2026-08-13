"""生产配置校验单元测试（方案 §6.3 / §11 第 7 条 / 评审 P1-4）。

缺配置、密钥文件缺失、非法 PEM、公私钥不匹配 → Settings 构造直接失败。
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.settings import Settings


def _keypair() -> tuple[str, str]:
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
    return private_pem, public_pem


@pytest.fixture(scope="module")
def keys(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    private_pem, public_pem = _keypair()
    keys_dir = tmp_path_factory.mktemp("prod_keys")
    private_file = keys_dir / "auth_private.pem"
    public_file = keys_dir / "auth_public.pem"
    private_file.write_text(private_pem, encoding="utf-8")
    public_file.write_text(public_pem, encoding="utf-8")
    return {
        "private_pem": private_pem,
        "public_pem": public_pem,
        "private_file": str(private_file),
        "public_file": str(public_file),
    }


def _base(keys: dict[str, str]) -> dict[str, object]:
    return {
        "app_env": "production",
        "dev_auth_enabled": False,
        "auth_private_key_file": keys["private_file"],
        "auth_public_key_file": keys["public_file"],
        "auth_database_url": "postgresql+psycopg://auth:auth@db:5432/auth",
    }


def test_production_with_complete_auth_config_passes(keys: dict[str, str]) -> None:
    Settings.model_validate(_base(keys))


def test_production_missing_private_key_fails(keys: dict[str, str]) -> None:
    base = _base(keys)
    base.pop("auth_private_key_file")
    with pytest.raises(ValueError, match="AUTH_PRIVATE_KEY_FILE"):
        Settings.model_validate(base)


def test_production_missing_public_key_fails(keys: dict[str, str]) -> None:
    base = _base(keys)
    base.pop("auth_public_key_file")
    with pytest.raises(ValueError, match="AUTH_PUBLIC_KEY_FILE / AUTH_PUBLIC_KEY / AUTH_JWKS_URL"):
        Settings.model_validate(base)


def test_production_missing_auth_database_url_fails(keys: dict[str, str]) -> None:
    base = _base(keys)
    base.pop("auth_database_url")
    with pytest.raises(ValueError, match="AUTH_DATABASE_URL"):
        Settings.model_validate(base)


def test_production_dev_auth_forbidden(keys: dict[str, str]) -> None:
    base = _base(keys)
    base["dev_auth_enabled"] = True
    with pytest.raises(ValueError, match="DEV_AUTH_ENABLED"):
        Settings.model_validate(base)


def test_production_private_key_file_missing_fails(keys: dict[str, str], tmp_path: object) -> None:
    base = _base(keys)
    base["auth_private_key_file"] = str(tmp_path) + "/nope.pem"
    with pytest.raises(ValueError, match="私钥文件不存在"):
        Settings.model_validate(base)


def test_production_invalid_private_key_pem_fails(keys: dict[str, str], tmp_path: object) -> None:
    from pathlib import Path

    bogus = Path(str(tmp_path)) / "bogus.pem"
    bogus.write_text("not-a-pem", encoding="utf-8")
    base = _base(keys)
    base["auth_private_key_file"] = str(bogus)
    with pytest.raises(ValueError, match="合法 PEM"):
        Settings.model_validate(base)


def test_production_key_pair_mismatch_fails(keys: dict[str, str], tmp_path: object) -> None:
    from pathlib import Path

    _, other_public = _keypair()
    other_file = Path(str(tmp_path)) / "other_public.pem"
    other_file.write_text(other_public, encoding="utf-8")
    base = _base(keys)
    base["auth_public_key_file"] = str(other_file)
    with pytest.raises(ValueError, match="私钥与公钥不匹配"):
        Settings.model_validate(base)


def test_production_jwks_only_allowed(keys: dict[str, str]) -> None:
    base = _base(keys)
    base.pop("auth_public_key_file")
    base["auth_jwks_url"] = "https://auth.example/.well-known/jwks.json"
    Settings.model_validate(base)
