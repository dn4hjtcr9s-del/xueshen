"""生产配置校验单元测试（方案 §6.3 / §11 第 7 条）：缺配置 → 启动失败。"""

from __future__ import annotations

import pytest

from backend.settings import Settings

_PRIVATE_FILE = "/tmp/gewu-test-auth_private.pem"


def _base() -> dict[str, object]:
    return {
        "app_env": "production",
        "dev_auth_enabled": False,
        "auth_private_key_file": _PRIVATE_FILE,
        "auth_public_key_file": "/tmp/gewu-test-auth_public.pem",
        "auth_database_url": "postgresql+psycopg://auth:auth@db:5432/auth",
    }


def test_production_with_complete_auth_config_passes() -> None:
    Settings.model_validate(_base())


def test_production_missing_private_key_fails() -> None:
    base = _base()
    base.pop("auth_private_key_file")
    with pytest.raises(ValueError, match="AUTH_PRIVATE_KEY_FILE"):
        Settings.model_validate(base)


def test_production_missing_public_key_fails() -> None:
    base = _base()
    base.pop("auth_public_key_file")
    with pytest.raises(ValueError, match="AUTH_PUBLIC_KEY_FILE / AUTH_PUBLIC_KEY / AUTH_JWKS_URL"):
        Settings.model_validate(base)


def test_production_missing_auth_database_url_fails() -> None:
    base = _base()
    base.pop("auth_database_url")
    with pytest.raises(ValueError, match="AUTH_DATABASE_URL"):
        Settings.model_validate(base)


def test_production_dev_auth_forbidden() -> None:
    base = _base()
    base["dev_auth_enabled"] = True
    with pytest.raises(ValueError, match="DEV_AUTH_ENABLED"):
        Settings.model_validate(base)
