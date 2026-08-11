"""应用设置：所有环境变量集中定义（规格 §14.7 / §16.3 / §11.5 / §14.1）。"""

from __future__ import annotations

from functools import lru_cache
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
        default="postgresql+psycopg://memory:memory@127.0.0.1:5432/memory",
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
    memory_allowed_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173"], alias="MEMORY_ALLOWED_ORIGINS"
    )

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
    service_token_audience: str = Field(default="memory-api", alias="SERVICE_TOKEN_AUDIENCE")
    auth_token_max_lifetime_seconds: int = Field(default=300)

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
        """生产环境安全约束（§18.1）。"""
        if self.app_env == "production":
            if self.dev_auth_enabled or self.dev_auth_allow_scope_override:
                raise ValueError("生产环境禁止 DEV_AUTH_ENABLED / DEV_AUTH_ALLOW_SCOPE_OVERRIDE")
        return self

    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    def production_auth_ready(self) -> bool:
        """生产 readiness：认证参数是否齐备（§2.1）。"""
        if not self.auth_issuer:
            return False
        return bool(self.auth_jwks_url or self.auth_public_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
