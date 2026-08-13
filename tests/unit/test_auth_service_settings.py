"""生产配置校验单元测试（方案 §6.3 / §11 第 7 条 / 评审 P1-4 / 复审 P1-1、P2-6）。

缺配置、密钥文件缺失、非法 PEM、公私钥不匹配、RSA 位数不足、
文件权限过宽、JWKS-only 配置 → Settings 构造直接失败。
"""

from __future__ import annotations

import os

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from backend.settings import Settings


def _keypair(key_size: int = 2048) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
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
    private_file.chmod(0o600)
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
        "auth_issuer": "gewu-auth",
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


def test_production_missing_issuer_fails(keys: dict[str, str]) -> None:
    """复审 P3：生产必须显式配置 AUTH_ISSUER（否则 iss 校验被静默跳过）。"""
    base = _base(keys)
    base.pop("auth_issuer")
    with pytest.raises(ValueError, match="AUTH_ISSUER"):
        Settings.model_validate(base)


def test_production_missing_public_key_fails(keys: dict[str, str]) -> None:
    base = _base(keys)
    base.pop("auth_public_key_file")
    with pytest.raises(ValueError, match="AUTH_PUBLIC_KEY_FILE / AUTH_PUBLIC_KEY"):
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
    bogus.chmod(0o600)
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


def test_production_jwks_only_rejected(keys: dict[str, str]) -> None:
    """复审 P1-1：第一版 token 无 kid，生产禁止 JWKS-only 配置。"""
    base = _base(keys)
    base.pop("auth_public_key_file")
    base["auth_jwks_url"] = "https://auth.example/.well-known/jwks.json"
    with pytest.raises(ValueError, match="AUTH_JWKS_URL"):
        Settings.model_validate(base)


@pytest.mark.skipif(os.name != "posix", reason="权限校验仅 POSIX")
@pytest.mark.parametrize("bad_mode", [0o644, 0o700, 0o400])
def test_production_private_key_permissions_not_exactly_0600_rejected(
    keys: dict[str, str], bad_mode: int
) -> None:
    """复审 P2：私钥权限必须精确 0600（过宽 0644/0700、过严 0400 均拒绝）。"""
    from pathlib import Path

    private_path = Path(keys["private_file"])
    original_mode = private_path.stat().st_mode
    try:
        private_path.chmod(bad_mode)
        with pytest.raises(ValueError, match="精确为 0600"):
            Settings.model_validate(_base(keys))
    finally:
        private_path.chmod(original_mode)


def test_production_private_key_size_1024_rejected(keys: dict[str, str], tmp_path: object) -> None:
    """复审 P2-6：非 2048 位 RSA 私钥启动拒绝。"""
    from pathlib import Path

    private_pem, public_pem = _keypair(key_size=1024)
    private_file = Path(str(tmp_path)) / "small_private.pem"
    public_file = Path(str(tmp_path)) / "small_public.pem"
    private_file.write_text(private_pem, encoding="utf-8")
    private_file.chmod(0o600)
    public_file.write_text(public_pem, encoding="utf-8")
    base = _base(keys)
    base["auth_private_key_file"] = str(private_file)
    base["auth_public_key_file"] = str(public_file)
    with pytest.raises(ValueError, match="RSA 2048"):
        Settings.model_validate(base)
