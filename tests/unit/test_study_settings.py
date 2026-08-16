"""Study Settings 单元测试（方案 §19/§21，Phase 0）。

验证：
- Feature Flags 默认全部关闭（"实现不等于批准启用"）；
- STUDY_DATABASE_URL 默认空（未配置不挂载路由，§21）；
- study_flags 快照字段与默认值；
- 生产环境校验：启用 Study 域必须显式配置数据库 URL 与三个模型角色。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from backend.settings import Settings


def _settings(env: str = "development", **overrides: object) -> Settings:
    return Settings(app_env=env, _env_file=None, **overrides)


def _rsa_keys() -> tuple[str, str]:
    """生产认证校验用 RSA 2048 密钥对（与 test_api_auth.py 同模式）。"""
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


def _prod_settings(tmp_path: Path, **overrides: object) -> Settings:
    """满足生产认证强校验（RSA2048/0600/公钥匹配）的 Settings 基座。"""
    private_pem, public_pem = _rsa_keys()
    private_file = tmp_path / "auth_private.pem"
    private_file.write_text(private_pem, encoding="utf-8")
    private_file.chmod(0o600)
    return Settings(
        app_env="production",
        dev_auth_enabled=False,
        auth_issuer="https://auth.example",
        auth_public_key=public_pem,
        auth_private_key_file=str(private_file),
        auth_database_url="postgresql+psycopg://auth:auth@db:5432/auth",
        memory_storage_root=str(tmp_path / "storage"),
        _env_file=None,
        **overrides,
    )


def test_study_feature_flags_default_false() -> None:
    """§19：Study 全部 Feature Flags 默认关闭。"""
    settings = _settings()
    assert settings.study_domain_enabled is False
    assert settings.study_memory_read_enabled is False
    assert settings.study_daily_feed_enabled is False
    assert settings.study_auto_replan_enabled is False
    assert settings.study_memory_writeback_enabled is False
    assert settings.study_notification_enabled is False


def test_study_database_url_defaults_empty() -> None:
    """§21：默认未配置 = 不挂载 Study 路由，进程不启动失败。"""
    settings = _settings()
    assert settings.study_database_url == ""


def test_study_flags_snapshot() -> None:
    """study_flags 快照包含全部六个开关（§19：启动时读入，运行中不热切换）。"""
    flags = _settings().study_flags
    assert flags == {
        "domain_enabled": False,
        "memory_read": False,
        "daily_feed": False,
        "auto_replan": False,
        "memory_writeback": False,
        "notification": False,
    }


def test_study_config_defaults() -> None:
    """§19 冻结的默认值：300s 扫描、7 天幂等、30 天模型缓存、intake/session 阈值。"""
    settings = _settings()
    assert settings.study_daily_feed_scan_interval_seconds == 300
    assert settings.study_idempotency_retention_days == 7
    assert settings.study_model_response_cache_retention_days == 30
    assert settings.study_intake_request_timeout_seconds == 8.0
    assert settings.study_intake_max_messages == 8
    assert settings.study_intake_message_max_chars == 2000
    assert settings.study_intake_ttl_hours == 24
    assert settings.study_session_heartbeat_seconds == 60
    assert settings.study_session_heartbeat_min_interval_seconds == 30
    assert settings.study_session_idle_timeout_seconds == 120


def test_production_study_enabled_requires_database_url(tmp_path: Path) -> None:
    """生产环境启用 Study 域但未配置数据库 URL → 构造失败（§21 fail-closed）。"""
    with pytest.raises(ValidationError, match="STUDY_DATABASE_URL"):
        _prod_settings(tmp_path, study_domain_enabled=True)


def test_production_study_enabled_requires_model_roles(tmp_path: Path) -> None:
    """生产环境启用 Study 域必须配置 intake/plan/feed 三个模型角色。"""
    with pytest.raises(ValidationError, match="openai_study_intake_model"):
        _prod_settings(
            tmp_path,
            study_domain_enabled=True,
            study_database_url="postgresql+psycopg://study:study@db:5432/study",
        )


def test_production_study_enabled_with_full_config_passes(tmp_path: Path) -> None:
    """URL + 三个模型角色齐备时生产构造通过。"""
    settings = _prod_settings(
        tmp_path,
        study_domain_enabled=True,
        study_database_url="postgresql+psycopg://study:study@db:5432/study",
        openai_study_intake_model="gpt-intake",
        openai_study_plan_model="gpt-plan",
        openai_study_feed_model="gpt-feed",
    )
    assert settings.study_domain_enabled is True
    assert settings.study_flags["domain_enabled"] is True


def test_production_study_disabled_needs_no_config(tmp_path: Path) -> None:
    """Study 域关闭时生产环境不要求任何 Study 配置（保持纯 Memory 部署可行）。"""
    _prod_settings(tmp_path)


def test_dev_study_enabled_without_models_allowed() -> None:
    """开发环境启用 Study 域不强制模型角色（Phase 1 manual 路径无模型可验收）。"""
    settings = _settings(study_domain_enabled=True, study_database_url="postgresql://x")
    assert settings.study_domain_enabled is True
    assert settings.openai_study_intake_model == ""
