"""应用设置：所有环境变量集中定义（规格 §14.7 / §16.3 / §11.5 / §14.1）。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # 基础
    app_env: Literal["development", "test", "staging", "production"] = Field(
        default="development", alias="APP_ENV"
    )
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # 数据库
    database_url: str = Field(
        default="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory",
        alias="DATABASE_URL",
    )
    database_statement_timeout_ms: int = Field(
        default=150_000, alias="DATABASE_STATEMENT_TIMEOUT_MS"
    )
    database_lock_timeout_ms: int = Field(default=10_000, alias="DATABASE_LOCK_TIMEOUT_MS")

    # 存储与图谱
    memory_storage_root: str = Field(default=".local/memory", alias="MEMORY_STORAGE_ROOT")
    knowledge_graph_root: str = Field(default="knowledge_graph", alias="KNOWLEDGE_GRAPH_ROOT")

    # API 进程
    memory_api_host: str = Field(default="127.0.0.1", alias="MEMORY_API_HOST")
    memory_api_port: int = Field(default=8000, alias="MEMORY_API_PORT")
    # §18.5：默认为空 —— 开发环境由 app.py 回落到 http://localhost:5173，生产默认关闭跨域
    memory_allowed_origins: list[str] = Field(default_factory=list, alias="MEMORY_ALLOWED_ORIGINS")

    # Worker / Scheduler / Outbox（§14.1 / §11.5 / §14.4）
    memory_worker_concurrency: int = Field(default=4, alias="MEMORY_WORKER_CONCURRENCY")
    memory_operation_lease_seconds: int = Field(default=120, alias="MEMORY_OPERATION_LEASE_SECONDS")
    memory_heartbeat_interval_seconds: int = Field(default=30)
    memory_operation_soft_timeout_seconds: int = Field(default=150)
    memory_operation_hard_timeout_seconds: int = Field(default=180)
    memory_worker_batch_size: int = Field(default=10)
    memory_worker_poll_seconds: float = Field(default=1.0)
    memory_worker_graceful_shutdown_seconds: int = Field(default=30)
    memory_outbox_poll_seconds: float = Field(default=1.0, alias="MEMORY_OUTBOX_POLL_SECONDS")
    memory_outbox_batch_size: int = Field(default=100)
    memory_outbox_lease_seconds: int = Field(default=60)
    memory_outbox_max_attempts: int = Field(default=10)
    memory_scheduler_timezone: str = Field(
        default="Asia/Shanghai", alias="MEMORY_SCHEDULER_TIMEZONE"
    )

    # 备份（§14.7 / §21.4）
    backup_root: str = Field(default=".local/backups", alias="BACKUP_ROOT")
    backup_encryption_method: str = Field(default="age-x25519-v1", alias="BACKUP_ENCRYPTION_METHOD")
    backup_age_recipient: str | None = Field(default=None, alias="BACKUP_AGE_RECIPIENT")
    backup_age_identity_file: str | None = Field(default=None, alias="BACKUP_AGE_IDENTITY_FILE")

    # OpenAI（§9.1）
    openai_api_key: str | None = Field(default=None, alias="OPENAI_API_KEY")
    openai_base_url: str | None = Field(default=None, alias="OPENAI_BASE_URL")
    openai_memory_model: str = Field(default="gpt-5.6-luna", alias="OPENAI_MEMORY_MODEL")
    openai_reasoning_effort: str = Field(default="none", alias="OPENAI_REASONING_EFFORT")
    openai_memory_timeout_seconds: float = Field(
        default=45.0, alias="OPENAI_MEMORY_TIMEOUT_SECONDS"
    )

    # 认证（§18.1）
    auth_issuer: str | None = Field(default=None, alias="AUTH_ISSUER")
    auth_audience: str = Field(default="memory-api", alias="AUTH_AUDIENCE")
    auth_jwks_url: str | None = Field(default=None, alias="AUTH_JWKS_URL")
    auth_public_key: str | None = Field(default=None, alias="AUTH_PUBLIC_KEY")
    # 文件优先于 PEM 文本（方案 §6.2）；文本形式保留用于测试直注
    auth_public_key_file: str | None = Field(default=None, alias="AUTH_PUBLIC_KEY_FILE")
    auth_private_key_file: str | None = Field(default=None, alias="AUTH_PRIVATE_KEY_FILE")
    service_token_audience: str = Field(default="memory-api", alias="SERVICE_TOKEN_AUDIENCE")
    auth_token_max_lifetime_seconds: int = Field(default=300)

    # 认证服务（签发方，方案 §6.2 / 附录 A.1 #5 / A.2 #9）
    auth_database_url: str = Field(
        default="postgresql+psycopg://auth:auth@127.0.0.1:55432/auth",
        alias="AUTH_DATABASE_URL",
    )
    # 可信代理 CIDR 白名单；默认空 = 认证端点仅信任直连地址
    auth_trusted_proxy_cidrs: list[str] = Field(
        default_factory=list, alias="AUTH_TRUSTED_PROXY_CIDRS"
    )

    # Dev Auth（仅 development；§18.1）
    dev_auth_enabled: bool = Field(default=True, alias="DEV_AUTH_ENABLED")
    dev_auth_allow_scope_override: bool = Field(
        default=False, alias="DEV_AUTH_ALLOW_SCOPE_OVERRIDE"
    )

    # Break-glass（§13.15）
    break_glass_enabled: bool = Field(default=True, alias="BREAK_GLASS_ENABLED")
    break_glass_max_minutes: int = Field(default=60)

    # HMAC 密钥分离（§18.1 / §19.9）
    log_hmac_key: str = Field(default="dev-log-hmac-key", alias="LOG_HMAC_KEY")
    privacy_hmac_key: str = Field(default="dev-privacy-hmac-key", alias="PRIVACY_HMAC_KEY")
    privacy_hmac_key_version: str = Field(default="v1", alias="PRIVACY_HMAC_KEY_VERSION")
    cursor_hmac_key: str = Field(default="dev-cursor-hmac-key", alias="CURSOR_HMAC_KEY")
    cursor_ttl_seconds: int = Field(default=900)

    # 图谱推导策略（§16.3，必须可配置，不得写死）
    graph_evidence_window_days: int = Field(default=180, alias="GRAPH_EVIDENCE_WINDOW_DAYS")
    graph_expert_min_span_days: int = Field(default=14, alias="GRAPH_EXPERT_MIN_SPAN_DAYS")
    graph_user_action_grace_hours: int = Field(default=72, alias="GRAPH_USER_ACTION_GRACE_HOURS")
    graph_strong_conflict_strength: float = Field(
        default=0.85, alias="GRAPH_STRONG_CONFLICT_STRENGTH"
    )
    graph_positive_strength: float = Field(default=0.70, alias="GRAPH_POSITIVE_STRENGTH")
    graph_projection_mapping_min: float = Field(default=0.92, alias="GRAPH_PROJECTION_MAPPING_MIN")
    graph_projection_mapping_margin: float = Field(
        default=0.15, alias="GRAPH_PROJECTION_MAPPING_MARGIN"
    )

    # 总结记忆阈值（§9.3，集中在 settings/policy）
    memory_auto_write_confidence: float = Field(default=0.80)
    memory_review_min_confidence: float = Field(default=0.55)
    memory_auto_merge_trgm: float = Field(default=0.72)
    memory_topic_conflict_trgm_min: float = Field(default=0.55)
    memory_tombstone_days: int = Field(default=30)
    memory_notification_retention_days: int = Field(default=90)
    memory_orphan_version_cleanup_hours: int = Field(default=24)
    memory_context_token_budget: int = Field(default=3000)

    # 限流（§18.5）
    rate_limit_write_per_minute: int = Field(default=30)
    rate_limit_search_per_minute: int = Field(default=60)
    rate_limit_graph_state_per_minute: int = Field(default=30)

    @model_validator(mode="after")
    def validate_environment_rules(self) -> Settings:
        """生产环境安全约束（§18.1 / §6.3）。"""
        if self.app_env == "production":
            if self.dev_auth_enabled or self.dev_auth_allow_scope_override:
                raise ValueError("生产环境禁止 DEV_AUTH_ENABLED / DEV_AUTH_ALLOW_SCOPE_OVERRIDE")
            # §6.3 评审 #14 / 复审 P1-1：签发/验签与 auth 库配置缺失 → 启动直接失败。
            # 第一版 token 不带 kid（方案 §6.2），JWKS 无法选中密钥，
            # 因此生产环境不允许仅配置 AUTH_JWKS_URL，必须提供本地匹配公钥。
            missing: list[str] = []
            if not self.auth_private_key_file:
                missing.append("AUTH_PRIVATE_KEY_FILE")
            if not (self.auth_public_key or self.auth_public_key_file):
                missing.append(
                    "AUTH_PUBLIC_KEY_FILE / AUTH_PUBLIC_KEY"
                    "（第一版 token 无 kid，生产不支持仅配置 AUTH_JWKS_URL）"
                )
            if "auth_database_url" not in self.model_fields_set:
                missing.append("AUTH_DATABASE_URL")
            if missing:
                raise ValueError("生产环境缺少认证服务配置: " + ", ".join(missing))
            self._validate_auth_keys()
        return self

    def _validate_auth_keys(self) -> None:
        """评审 P1-4 / 复审 P1-1、P2-6：生产启动时实际校验密钥。

        - 私钥：存在、可读、合法 PEM、RSA 2048 位（方案 §6.2）、权限 0600；
        - 公钥：文件优先、PEM 文本备选；与私钥必须匹配；
        - 第一版 token 无 kid（方案 §6.2），生产禁止仅配置 AUTH_JWKS_URL。
        """
        import os

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa as crypto_rsa

        private_path = Path(self.auth_private_key_file or "")
        if not private_path.is_file():
            raise ValueError(f"私钥文件不存在或不可读: {private_path}")
        # 复审 P2-6：私钥文件不得对 group/other 开放任何权限（POSIX）
        if os.name == "posix":
            mode = private_path.stat().st_mode & 0o077
            if mode:
                raise ValueError(
                    f"私钥文件权限过宽（方案要求 0600，当前 {oct(mode)}）: {private_path}"
                )
        try:
            private_key = serialization.load_pem_private_key(
                private_path.read_bytes(), password=None
            )
        except Exception as exc:
            raise ValueError(f"私钥文件不是合法 PEM 私钥: {private_path}") from exc
        if not isinstance(private_key, crypto_rsa.RSAPrivateKey):
            raise ValueError("私钥必须是 RSA 密钥")
        # 复审 P2-6：强制方案指定的 RSA 2048 位
        if private_key.key_size != 2048:
            raise ValueError(f"私钥必须为 RSA 2048 位（方案 §6.2），当前 {private_key.key_size} 位")

        public_pem: str | None = None
        if self.auth_public_key_file:
            public_path = Path(self.auth_public_key_file)
            if not public_path.is_file():
                raise ValueError(f"公钥文件不存在或不可读: {public_path}")
            public_pem = public_path.read_text(encoding="utf-8")
        elif self.auth_public_key:
            public_pem = self.auth_public_key
        else:
            # 复审 P1-1：无 kid 的 token 无法被 JWKS 选中密钥，
            # 生产不允许 JWKS-only 配置（missing 检查已先行拒绝，此处兜底）
            raise ValueError("第一版 token 无 kid，生产环境不允许仅配置 AUTH_JWKS_URL")
        try:
            public_key = serialization.load_pem_public_key(public_pem.encode("ascii"))
        except Exception as exc:
            raise ValueError("公钥不是合法 PEM") from exc
        der = serialization.Encoding.DER
        fmt = serialization.PublicFormat.SubjectPublicKeyInfo
        if public_key.public_bytes(der, fmt) != private_key.public_key().public_bytes(der, fmt):
            raise ValueError("私钥与公钥不匹配")

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    def production_auth_ready(self) -> bool:
        """生产 readiness：认证参数是否齐备（§2.1 / 复审 P1-1 要求本地公钥）。"""
        if not self.auth_issuer:
            return False
        return bool(self.auth_public_key or self.auth_public_key_file)


@lru_cache
def get_settings() -> Settings:
    return Settings()
