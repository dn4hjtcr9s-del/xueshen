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

    # ------------------------------------------------------------------
    # Conversation Agentic RAG（方案 §20，v1.3/v1.4）
    # ------------------------------------------------------------------

    # 数据与运行时（§20.1）
    conversation_database_url: str = Field(
        default="postgresql+psycopg://conversation:conversation@127.0.0.1:55432/conversation",
        alias="CONVERSATION_DATABASE_URL",
    )
    conversation_graph_checkpoint_schema: str = Field(
        default="conversation_checkpoints", alias="CONVERSATION_GRAPH_CHECKPOINT_SCHEMA"
    )
    conversation_max_active_turns_per_thread: int = Field(
        default=1, alias="CONVERSATION_MAX_ACTIVE_TURNS_PER_THREAD"
    )
    conversation_worker_poll_seconds: float = Field(
        default=1.0, alias="CONVERSATION_WORKER_POLL_SECONDS"
    )
    conversation_turn_lease_seconds: int = Field(
        default=60, alias="CONVERSATION_TURN_LEASE_SECONDS"
    )
    conversation_turn_max_attempts: int = Field(default=3, alias="CONVERSATION_TURN_MAX_ATTEMPTS")
    conversation_job_max_attempts: int = Field(default=10, alias="CONVERSATION_JOB_MAX_ATTEMPTS")
    conversation_retention_sweep_interval_hours: int = Field(
        default=24, alias="CONVERSATION_RETENTION_SWEEP_INTERVAL_HOURS"
    )
    conversation_max_user_message_chars: int = Field(
        default=10_000, alias="CONVERSATION_MAX_USER_MESSAGE_CHARS"
    )
    conversation_list_default_limit: int = Field(
        default=50, alias="CONVERSATION_LIST_DEFAULT_LIMIT"
    )
    conversation_list_max_limit: int = Field(default=100, alias="CONVERSATION_LIST_MAX_LIMIT")
    conversation_turn_rate_limit_per_minute: int = Field(
        default=10, alias="CONVERSATION_TURN_RATE_LIMIT_PER_MINUTE"
    )
    conversation_internal_tester_user_ids: list[str] = Field(
        default_factory=list, alias="CONVERSATION_INTERNAL_TESTER_USER_IDS"
    )
    conversation_context_max_messages: int = Field(
        default=20, alias="CONVERSATION_CONTEXT_MAX_MESSAGES"
    )
    conversation_context_token_budget: int = Field(
        default=6000, alias="CONVERSATION_CONTEXT_TOKEN_BUDGET"
    )
    conversation_memory_token_budget: int = Field(
        default=3000, alias="CONVERSATION_MEMORY_TOKEN_BUDGET"
    )
    conversation_answer_token_budget: int = Field(
        default=2000, alias="CONVERSATION_ANSWER_TOKEN_BUDGET"
    )
    conversation_summary_trigger_tokens: int = Field(
        default=8000, alias="CONVERSATION_SUMMARY_TRIGGER_TOKENS"
    )
    conversation_turn_deadline_seconds: int = Field(
        default=120, alias="CONVERSATION_TURN_DEADLINE_SECONDS"
    )

    # RAG 与 Query Embedding（§20.2）
    conversation_retrieval_max_subqueries: int = Field(
        default=4, alias="CONVERSATION_RETRIEVAL_MAX_SUBQUERIES"
    )
    conversation_retrieval_max_total_subqueries: int = Field(
        default=6, alias="CONVERSATION_RETRIEVAL_MAX_TOTAL_SUBQUERIES"
    )
    conversation_retrieval_max_iterations: int = Field(
        default=1, alias="CONVERSATION_RETRIEVAL_MAX_ITERATIONS"
    )
    conversation_retrieval_concurrency: int = Field(
        default=4, alias="CONVERSATION_RETRIEVAL_CONCURRENCY"
    )
    conversation_retrieval_worker_timeout_seconds: float = Field(
        default=5.0, alias="CONVERSATION_RETRIEVAL_WORKER_TIMEOUT_SECONDS"
    )
    conversation_retrieval_total_timeout_seconds: float = Field(
        default=10.0, alias="CONVERSATION_RETRIEVAL_TOTAL_TIMEOUT_SECONDS"
    )
    conversation_retrieval_result_limit: int = Field(
        default=20, alias="CONVERSATION_RETRIEVAL_RESULT_LIMIT"
    )
    conversation_retrieval_adjacent_chunks_per_side: int = Field(
        default=1, alias="CONVERSATION_RETRIEVAL_ADJACENT_CHUNKS_PER_SIDE"
    )
    conversation_retrieval_merged_hit_max_tokens: int = Field(
        default=1500, alias="CONVERSATION_RETRIEVAL_MERGED_HIT_MAX_TOKENS"
    )
    conversation_citation_snippet_max_chars: int = Field(
        default=300, alias="CONVERSATION_CITATION_SNIPPET_MAX_CHARS"
    )
    conversation_evidence_token_budget: int = Field(
        default=4000, alias="CONVERSATION_EVIDENCE_TOKEN_BUDGET"
    )

    # Memory、Reader 与 Outbox（§20.3）
    memory_api_base_url: str = Field(default="", alias="MEMORY_API_BASE_URL")
    memory_agent_token: str | None = Field(default=None, alias="MEMORY_AGENT_TOKEN")
    memory_context_timeout_seconds: float = Field(
        default=5.0, alias="MEMORY_CONTEXT_TIMEOUT_SECONDS"
    )
    conversation_reader_base_url: str = Field(default="", alias="CONVERSATION_READER_BASE_URL")
    conversation_reader_service_token: str | None = Field(
        default=None, alias="CONVERSATION_READER_SERVICE_TOKEN"
    )
    conversation_source_delete_service_token: str | None = Field(
        default=None, alias="CONVERSATION_SOURCE_DELETE_SERVICE_TOKEN"
    )
    conversation_outbox_poll_seconds: float = Field(
        default=1.0, alias="CONVERSATION_OUTBOX_POLL_SECONDS"
    )
    conversation_outbox_lease_seconds: int = Field(
        default=60, alias="CONVERSATION_OUTBOX_LEASE_SECONDS"
    )
    conversation_outbox_max_attempts: int = Field(
        default=10, alias="CONVERSATION_OUTBOX_MAX_ATTEMPTS"
    )

    # SSE（§20.4）
    conversation_sse_heartbeat_seconds: int = Field(
        default=15, alias="CONVERSATION_SSE_HEARTBEAT_SECONDS"
    )
    conversation_sse_event_retention_days: int = Field(
        default=30, alias="CONVERSATION_SSE_EVENT_RETENTION_DAYS"
    )
    conversation_checkpoint_retention_days: int = Field(
        default=30, alias="CONVERSATION_CHECKPOINT_RETENTION_DAYS"
    )
    conversation_sse_delta_batch_ms: float = Field(
        default=100.0, alias="CONVERSATION_SSE_DELTA_BATCH_MS"
    )
    conversation_sse_delta_batch_chars: int = Field(
        default=64, alias="CONVERSATION_SSE_DELTA_BATCH_CHARS"
    )

    # 模型角色（§19.1：不写死模型名，按角色配置）
    openai_rewrite_model: str = Field(default="", alias="OPENAI_REWRITE_MODEL")
    openai_evidence_model: str = Field(default="", alias="OPENAI_EVIDENCE_MODEL")
    openai_answer_model: str = Field(default="", alias="OPENAI_ANSWER_MODEL")
    openai_conversation_summary_model: str = Field(
        default="", alias="OPENAI_CONVERSATION_SUMMARY_MODEL"
    )

    # ------------------------------------------------------------------
    # Community 社区（方案 community-implementation-plan.md v1.6，§4.2/§13.2）
    # ------------------------------------------------------------------

    # D25：默认空 = 未配置时不挂载 Community 路由（含写路径与内部 Reader/purge），
    # readiness 不报错、进程不启动失败；显式配置后按 readiness fail-closed。
    community_database_url: str = Field(default="", alias="COMMUNITY_DATABASE_URL")
    community_reader_base_url: str = Field(default="", alias="COMMUNITY_READER_BASE_URL")
    community_reader_service_token: str | None = Field(
        default=None, alias="COMMUNITY_READER_SERVICE_TOKEN"
    )
    community_source_delete_service_token: str | None = Field(
        default=None, alias="COMMUNITY_SOURCE_DELETE_SERVICE_TOKEN"
    )
    community_account_purge_service_token: str | None = Field(
        default=None, alias="COMMUNITY_ACCOUNT_PURGE_SERVICE_TOKEN"
    )
    # Feature Flags（§10.1/D7：初始默认关闭，按 deletion→evidence 顺序灰度；
    # 存在 token 不代表已批准启用链路）
    community_publisher_enabled: bool = Field(default=False, alias="COMMUNITY_PUBLISHER_ENABLED")
    community_memory_submit_enabled: bool = Field(
        default=False, alias="COMMUNITY_MEMORY_SUBMIT_ENABLED"
    )
    community_source_deletion_enabled: bool = Field(
        default=False, alias="COMMUNITY_SOURCE_DELETION_ENABLED"
    )
    # Outbox Publisher 与维护任务（D38：与 conversation 默认值对齐）
    community_outbox_poll_seconds: float = Field(default=1.0, alias="COMMUNITY_OUTBOX_POLL_SECONDS")
    community_outbox_lease_seconds: int = Field(default=60, alias="COMMUNITY_OUTBOX_LEASE_SECONDS")
    community_outbox_max_attempts: int = Field(default=10, alias="COMMUNITY_OUTBOX_MAX_ATTEMPTS")
    community_outbox_batch_size: int = Field(default=50, alias="COMMUNITY_OUTBOX_BATCH_SIZE")
    community_maintenance_interval_seconds: int = Field(
        default=3600, alias="COMMUNITY_MAINTENANCE_INTERVAL_SECONDS"
    )
    # 保留与清理（§12.4）
    community_idempotency_retention_days: int = Field(
        default=7, alias="COMMUNITY_IDEMPOTENCY_RETENTION_DAYS"
    )
    community_outbox_delivered_retention_days: int = Field(
        default=30, alias="COMMUNITY_OUTBOX_DELIVERED_RETENTION_DAYS"
    )
    community_outbox_dead_letter_retention_days: int = Field(
        default=90, alias="COMMUNITY_OUTBOX_DEAD_LETTER_RETENTION_DAYS"
    )
    community_notification_retention_days: int = Field(
        default=90, alias="COMMUNITY_NOTIFICATION_RETENTION_DAYS"
    )
    community_cleanup_batch_size: int = Field(default=500, alias="COMMUNITY_CLEANUP_BATCH_SIZE")
    # 内容限制（§6.2）
    community_post_body_max_length: int = Field(
        default=19_500, alias="COMMUNITY_POST_BODY_MAX_LENGTH"
    )
    community_reply_max_length: int = Field(default=19_500, alias="COMMUNITY_REPLY_MAX_LENGTH")
    # 限流（§9.3：墙钟固定窗口，D19 接受边界突发）
    community_rate_limit_post_per_hour: int = Field(
        default=10, alias="COMMUNITY_RATE_LIMIT_POST_PER_HOUR"
    )
    community_rate_limit_reply_per_minute: int = Field(
        default=5, alias="COMMUNITY_RATE_LIMIT_REPLY_PER_MINUTE"
    )
    community_rate_limit_reply_per_hour: int = Field(
        default=60, alias="COMMUNITY_RATE_LIMIT_REPLY_PER_HOUR"
    )
    community_rate_limit_like_per_minute: int = Field(
        default=60, alias="COMMUNITY_RATE_LIMIT_LIKE_PER_MINUTE"
    )
    community_rate_limit_read_per_minute: int = Field(
        default=120, alias="COMMUNITY_RATE_LIMIT_READ_PER_MINUTE"
    )
    # 可信代理 CIDR（D18：默认空 = 只信任直连对端）
    community_trusted_proxy_cidrs: list[str] = Field(
        default_factory=list, alias="COMMUNITY_TRUSTED_PROXY_CIDRS"
    )

    # ------------------------------------------------------------------
    # Study 学习编排（docs/study-plan-push-implementation-plan.md v1.2，§19/D1–D29）
    # ------------------------------------------------------------------

    # D25 同模式：默认空 = 未配置时不挂载 Study 路由（§21），readiness 不报错；
    # STUDY_DOMAIN_ENABLED=true 但未配置 URL 时 readiness fail-closed。
    study_database_url: str = Field(default="", alias="STUDY_DATABASE_URL")
    study_graph_checkpoint_schema: str = Field(
        default="study_checkpoints", alias="STUDY_GRAPH_CHECKPOINT_SCHEMA"
    )
    # Worker（D17：同用户串行、跨用户并发由 STUDY_WORKER_CONCURRENCY 控制）
    study_worker_concurrency: int = Field(default=4, alias="STUDY_WORKER_CONCURRENCY")
    study_worker_poll_seconds: float = Field(default=1.0, alias="STUDY_WORKER_POLL_SECONDS")
    study_operation_lease_seconds: int = Field(default=120, alias="STUDY_OPERATION_LEASE_SECONDS")
    study_operation_soft_timeout_seconds: int = Field(
        default=150, alias="STUDY_OPERATION_SOFT_TIMEOUT_SECONDS"
    )
    study_operation_hard_timeout_seconds: int = Field(
        default=180, alias="STUDY_OPERATION_HARD_TIMEOUT_SECONDS"
    )
    # Scheduler（D9：每 5 分钟按用户 IANA 时区兜底扫描；§19 调度进程自身时区）
    study_scheduler_timezone: str = Field(default="Asia/Shanghai", alias="STUDY_SCHEDULER_TIMEZONE")
    study_daily_feed_scan_interval_seconds: int = Field(
        default=300, alias="STUDY_DAILY_FEED_SCAN_INTERVAL_SECONDS"
    )
    study_daily_feed_scan_batch_size: int = Field(
        default=100, alias="STUDY_DAILY_FEED_SCAN_BATCH_SIZE"
    )
    # 幂等与模型缓存保留（D15/D16）
    study_idempotency_retention_days: int = Field(
        default=7, alias="STUDY_IDEMPOTENCY_RETENTION_DAYS"
    )
    study_model_response_cache_retention_days: int = Field(
        default=30, alias="STUDY_MODEL_RESPONSE_CACHE_RETENTION_DAYS"
    )
    # Intake（D10/§7.1：同步追问、8 轮/2,000 字符/24 小时上限）
    study_intake_request_timeout_seconds: float = Field(
        default=8.0, alias="STUDY_INTAKE_REQUEST_TIMEOUT_SECONDS"
    )
    study_intake_max_messages: int = Field(default=8, alias="STUDY_INTAKE_MAX_MESSAGES")
    study_intake_message_max_chars: int = Field(
        default=2000, alias="STUDY_INTAKE_MESSAGE_MAX_CHARS"
    )
    study_intake_ttl_hours: int = Field(default=24, alias="STUDY_INTAKE_TTL_HOURS")
    # Session heartbeat（§12.5/D28：60s 周期、30s 最小间隔、120s 空闲阈值）
    study_session_heartbeat_seconds: int = Field(
        default=60, alias="STUDY_SESSION_HEARTBEAT_SECONDS"
    )
    study_session_heartbeat_min_interval_seconds: int = Field(
        default=30, alias="STUDY_SESSION_HEARTBEAT_MIN_INTERVAL_SECONDS"
    )
    study_session_idle_timeout_seconds: int = Field(
        default=120, alias="STUDY_SESSION_IDLE_TIMEOUT_SECONDS"
    )
    # 内部 purge（D19：fail-closed，未配置 token 不挂载路由）
    study_account_purge_service_token: str | None = Field(
        default=None, alias="STUDY_ACCOUNT_PURGE_SERVICE_TOKEN"
    )
    # 模型角色（§19：不写死模型名，按角色配置；Phase 1 的 manual 路径不依赖模型）
    openai_study_intake_model: str = Field(default="", alias="OPENAI_STUDY_INTAKE_MODEL")
    openai_study_plan_model: str = Field(default="", alias="OPENAI_STUDY_PLAN_MODEL")
    openai_study_feed_model: str = Field(default="", alias="OPENAI_STUDY_FEED_MODEL")

    # Feature Flags（§19：全部默认关闭，"实现不等于批准启用"）
    study_domain_enabled: bool = Field(default=False, alias="STUDY_DOMAIN_ENABLED")
    study_memory_read_enabled: bool = Field(default=False, alias="STUDY_MEMORY_READ_ENABLED")
    study_daily_feed_enabled: bool = Field(default=False, alias="STUDY_DAILY_FEED_ENABLED")
    study_auto_replan_enabled: bool = Field(default=False, alias="STUDY_AUTO_REPLAN_ENABLED")
    study_memory_writeback_enabled: bool = Field(
        default=False, alias="STUDY_MEMORY_WRITEBACK_ENABLED"
    )
    study_notification_enabled: bool = Field(default=False, alias="STUDY_NOTIFICATION_ENABLED")

    # Embedding 复用现有配置（§20.2 / D15：不新增重复凭证）
    embedding_base_url: str | None = Field(default=None, alias="EMBEDDING_BASE_URL")
    embedding_api_key: str | None = Field(default=None, alias="EMBEDDING_API_KEY")
    embedding_model: str | None = Field(default=None, alias="EMBEDDING_MODEL")
    rag_embedding_dimensions: int = Field(default=1024, alias="RAG_EMBEDDING_DIMENSIONS")
    # §20.2 / D15：EMBEDDING_API_KEY 的既有回退源（scripts 域同规则）
    dashscope_api_key: str | None = Field(default=None, alias="DASHSCOPE_API_KEY")

    # Feature Flags（§27.1 / 附录 A.10）
    # §1.3（评审 P1-8）：内部 Memory transport 未获 owner 批准前默认关闭；
    # "实现不等批准，启用必须等批准"。Memory 读取/投递相关默认 false。
    conversation_agentic_rag_enabled: bool = Field(
        default=True, alias="CONVERSATION_AGENTIC_RAG_ENABLED"
    )
    conversation_multi_query_enabled: bool = Field(
        default=True, alias="CONVERSATION_MULTI_QUERY_ENABLED"
    )
    conversation_evidence_loop_enabled: bool = Field(
        default=True, alias="CONVERSATION_EVIDENCE_LOOP_ENABLED"
    )
    conversation_memory_read_enabled: bool = Field(
        default=False, alias="CONVERSATION_MEMORY_READ_ENABLED"
    )
    conversation_memory_submit_enabled: bool = Field(
        default=False, alias="CONVERSATION_MEMORY_SUBMIT_ENABLED"
    )
    conversation_streaming_enabled: bool = Field(
        default=True, alias="CONVERSATION_STREAMING_ENABLED"
    )

    @property
    def embedding_api_key_resolved(self) -> str | None:
        """§20.2 / D15：EMBEDDING_API_KEY 缺失时回退 DASHSCOPE_API_KEY。"""
        if self.embedding_api_key:
            return self.embedding_api_key
        return self.dashscope_api_key

    @property
    def conversation_flags(self) -> dict[str, bool]:
        """Feature Flag 快照（附录 A.10：进程启动时读入，运行中不热切换）。"""
        return {
            "agentic_rag": self.conversation_agentic_rag_enabled,
            "multi_query": self.conversation_multi_query_enabled,
            "evidence_loop": self.conversation_evidence_loop_enabled,
            "memory_read": self.conversation_memory_read_enabled,
            "memory_submit": self.conversation_memory_submit_enabled,
            "streaming": self.conversation_streaming_enabled,
        }

    @property
    def study_flags(self) -> dict[str, bool]:
        """Study Feature Flag 快照（§19：进程启动时读入，运行中不热切换）。"""
        return {
            "domain_enabled": self.study_domain_enabled,
            "memory_read": self.study_memory_read_enabled,
            "daily_feed": self.study_daily_feed_enabled,
            "auto_replan": self.study_auto_replan_enabled,
            "memory_writeback": self.study_memory_writeback_enabled,
            "notification": self.study_notification_enabled,
        }

    @model_validator(mode="after")
    def validate_environment_rules(self) -> Settings:
        """生产环境安全约束（§18.1 / §6.3）。"""
        if self.app_env == "production":
            if self.dev_auth_enabled or self.dev_auth_allow_scope_override:
                raise ValueError("生产环境禁止 DEV_AUTH_ENABLED / DEV_AUTH_ALLOW_SCOPE_OVERRIDE")
            # §6.3 评审 #14 / 复审 P1-1、P3：签发/验签与 auth 库配置缺失 → 启动直接失败。
            # 第一版 token 不带 kid（方案 §6.2），JWKS 无法选中密钥，
            # 因此生产环境不允许仅配置 AUTH_JWKS_URL，必须提供本地匹配公钥。
            missing: list[str] = []
            if not self.auth_issuer:
                missing.append("AUTH_ISSUER")
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
            # 附录 A.11 #2（评审 P2）：生产若显式启用 Conversation Agentic RAG 功能
            # （agentic_rag / memory_read / memory_submit 任一被显式开启，
            # 或显式提供了 CONVERSATION_DATABASE_URL），必须显式配置数据库 URL，
            # 禁止开发默认凭证。全部默认关闭/未显式启用时保持纯 Memory 部署可行。
            conversation_explicitly_used = (
                "conversation_database_url" in self.model_fields_set
                or any(
                    field in self.model_fields_set
                    for field in (
                        "conversation_agentic_rag_enabled",
                        "conversation_memory_read_enabled",
                        "conversation_memory_submit_enabled",
                    )
                )
            )
            if (
                conversation_explicitly_used
                and "conversation_database_url" not in self.model_fields_set
            ):
                raise ValueError(
                    "生产环境启用 Conversation 功能必须显式配置"
                    " CONVERSATION_DATABASE_URL（不允许使用开发默认凭证）"
                )
            # 附录 A.7 / 评审 P1-10：生产若启用 Agentic RAG 且提供 Conversation URL，
            # 模型角色必须齐备（未提供 URL = Conversation 未部署，角色校验无意义）
            if (
                self.conversation_agentic_rag_enabled
                and "conversation_database_url" in self.model_fields_set
            ):
                for role_field in (
                    "openai_rewrite_model",
                    "openai_evidence_model",
                    "openai_answer_model",
                ):
                    if not getattr(self, role_field, ""):
                        raise ValueError(f"生产环境启用 Agentic RAG 必须配置 {role_field}")
            # D46（镜像 conversation 规则）：显式配置 Community 且 submit/删除链路
            # 开启时，各自依赖必须齐备（§13.2 显式 bool 与 token presence 分离）：
            # - submit：Memory 需读社区来源 → reader base url + reader token；
            # - deletion：投递删除事实到本进程 Memory API → source_delete token
            #   + MEMORY_API_BASE_URL（投递目标地址）。
            # COMMUNITY_DATABASE_URL 缺失不强制（D25 不挂载语义，由 readiness 兜底）。
            if "community_database_url" in self.model_fields_set:
                if self.community_memory_submit_enabled:
                    if not self.community_reader_base_url:
                        raise ValueError(
                            "生产环境启用 Community 证据链路必须配置 COMMUNITY_READER_BASE_URL"
                        )
                    if not self.community_reader_service_token:
                        raise ValueError(
                            "生产环境启用 Community 证据链路必须配置 COMMUNITY_READER_SERVICE_TOKEN"
                        )
                if self.community_source_deletion_enabled:
                    if not self.community_source_delete_service_token:
                        raise ValueError(
                            "生产环境启用 Community 删除链路必须配置"
                            " COMMUNITY_SOURCE_DELETE_SERVICE_TOKEN"
                        )
                    if not self.memory_api_base_url:
                        raise ValueError(
                            "生产环境启用 Community 删除链路必须配置 MEMORY_API_BASE_URL"
                        )
            # Study（方案 §19/§21：启用 Study 域必须显式配置数据库 URL 与模型角色，
            # 禁止开发默认凭证进入生产）
            if self.study_domain_enabled:
                if not self.study_database_url:
                    raise ValueError("生产环境启用 Study 域必须显式配置 STUDY_DATABASE_URL")
                for role_field in (
                    "openai_study_intake_model",
                    "openai_study_plan_model",
                    "openai_study_feed_model",
                ):
                    if not getattr(self, role_field, ""):
                        raise ValueError(f"生产环境启用 Study 域必须配置 {role_field}")
        return self

    def _validate_auth_keys(self) -> None:
        """评审 P1-4 / 复审 P1-1、P2-6：生产启动时实际校验密钥。

        - 私钥：存在、可读、合法 PEM、RSA 2048 位（方案 §6.2）、权限 0600；
        - 公钥：文件优先、PEM 文本备选；与私钥必须匹配；
        - 第一版 token 无 kid（方案 §6.2），生产禁止仅配置 AUTH_JWKS_URL。
        """
        import os
        import stat

        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa as crypto_rsa

        private_path = Path(self.auth_private_key_file or "")
        if not private_path.is_file():
            raise ValueError(f"私钥文件不存在或不可读: {private_path}")
        # 复审 P2：私钥文件权限精确强制 0600（方案 §6.2），过宽或过严均拒绝
        if os.name == "posix":
            mode = stat.S_IMODE(private_path.stat().st_mode)
            if mode != 0o600:
                raise ValueError(
                    f"私钥文件权限必须精确为 0600（方案 §6.2），当前 {oct(mode)}: {private_path}"
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
