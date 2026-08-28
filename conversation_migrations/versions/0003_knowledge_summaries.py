"""冻结 KnowledgeSummary 的 Conversation 专属 DDL（知识总结方案 §7）。

本迁移只创建 Conversation 数据库中的知识总结表、索引和 Turn enqueue 修复字段；
不读取、不修改 Memory、知识图谱、RAG、Auth、Community 或 Study 数据。`pg_trgm`
扩展是关键词搜索与候选召回的必要前置条件，创建失败必须使迁移整体失败。
"""

from __future__ import annotations

from alembic import op

revision = "0003_knowledge_summaries"
down_revision = "0002_checkpoint_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建知识总结的全部冻结表、外键、约束和索引。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Turn 只保存自动 enqueue 的可修复投影，不保存单个 Generation ID。
    op.execute(
        """
        ALTER TABLE conversation.conversation_turns
            ADD COLUMN knowledge_summary_enqueue_status text NOT NULL DEFAULT 'not_requested'
                CHECK (knowledge_summary_enqueue_status IN
                    ('not_requested', 'pending', 'enqueued', 'enqueue_failed')),
            ADD COLUMN knowledge_summary_enqueue_attempts integer NOT NULL DEFAULT 0
                CHECK (knowledge_summary_enqueue_attempts >= 0),
            ADD COLUMN knowledge_summary_enqueue_next_attempt_at timestamptz
        """
    )

    # ------------------------------------------------------------------
    # 当前总结快照与标题别名（方案 §7.1 / §7.2）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE conversation.knowledge_summaries (
            summary_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            topic_group_title varchar(160) NOT NULL,
            topic_title varchar(240) NOT NULL,
            normalized_topic_group varchar(160) NOT NULL,
            normalized_topic_title varchar(240) NOT NULL,
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'deleted')),
            review_state text NOT NULL DEFAULT 'clean'
                CHECK (review_state IN ('clean', 'possible_duplicate', 'conflict')),
            content_schema_version smallint NOT NULL DEFAULT 1
                CHECK (content_schema_version = 1),
            content jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'),
            search_text text NOT NULL CHECK (length(search_text) <= 30000),
            protected_sections text[] NOT NULL DEFAULT '{}'
                CHECK (protected_sections <@ ARRAY[
                    'overview', 'definitions', 'theorems', 'formulas',
                    'properties', 'methods', 'pitfalls'
                ]::text[]),
            version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
            source_count integer NOT NULL DEFAULT 0 CHECK (source_count >= 0),
            available_source_count integer NOT NULL DEFAULT 0
                CHECK (available_source_count >= 0 AND available_source_count <= source_count),
            source_message_count integer NOT NULL DEFAULT 0 CHECK (source_message_count >= 0),
            content_hash char(64) NOT NULL,
            state_hash char(64) NOT NULL,
            last_generation_id uuid,
            last_generated_at timestamptz,
            merged_into_summary_id uuid REFERENCES conversation.knowledge_summaries(summary_id)
                ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz,
            CHECK (
                (status = 'active' AND deleted_at IS NULL)
                OR (status = 'deleted' AND deleted_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summaries_user_updated
        ON conversation.knowledge_summaries (user_id, updated_at DESC, summary_id DESC)
        WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_knowledge_summaries_exact_topic
        ON conversation.knowledge_summaries (
            user_id, normalized_topic_group, normalized_topic_title
        )
        WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summaries_group
        ON conversation.knowledge_summaries (user_id, normalized_topic_group, updated_at DESC)
        WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summaries_search_trgm
        ON conversation.knowledge_summaries
        USING gin (search_text gin_trgm_ops)
        WHERE status = 'active'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summaries_title_trgm
        ON conversation.knowledge_summaries
        USING gin (normalized_topic_title gin_trgm_ops)
        WHERE status = 'active'
        """
    )

    op.execute(
        """
        CREATE TABLE conversation.knowledge_summary_aliases (
            alias_id uuid PRIMARY KEY,
            summary_id uuid NOT NULL REFERENCES conversation.knowledge_summaries(summary_id)
                ON DELETE CASCADE,
            user_id uuid NOT NULL,
            normalized_topic_group varchar(160) NOT NULL,
            display_alias varchar(240) NOT NULL,
            normalized_alias varchar(240) NOT NULL,
            created_by text NOT NULL CHECK (created_by IN ('system', 'model', 'user')),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (summary_id, normalized_alias)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_alias_lookup
        ON conversation.knowledge_summary_aliases
        (user_id, normalized_topic_group, normalized_alias)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_alias_trgm
        ON conversation.knowledge_summary_aliases
        USING gin (normalized_alias gin_trgm_ops)
        """
    )

    # ------------------------------------------------------------------
    # 可靠 Generation Job 与脱敏模型调用审计（方案 §7.5 / §7.6）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE conversation.knowledge_summary_generation_jobs (
            generation_id uuid PRIMARY KEY,
            idempotency_key varchar(500) NOT NULL UNIQUE,
            client_request_id varchar(200),
            user_id uuid NOT NULL,
            thread_id uuid NOT NULL REFERENCES conversation.conversation_threads(thread_id)
                ON DELETE RESTRICT,
            turn_id uuid NOT NULL REFERENCES conversation.conversation_turns(turn_id)
                ON DELETE RESTRICT,
            source_checkpoint_id varchar(500) NOT NULL,
            trigger text NOT NULL CHECK (trigger IN
                ('auto', 'manual', 'manual_refresh', 'manual_retry', 'ops_retry')),
            status text NOT NULL DEFAULT 'pending' CHECK (status IN
                ('pending', 'processing', 'retry_wait', 'succeeded', 'no_change',
                 'needs_review', 'dead_letter', 'cancelled')),
            input_manifest jsonb,
            extraction_result jsonb,
            merge_plan_result jsonb,
            affected_summary_ids uuid[] NOT NULL DEFAULT '{}',
            warning_codes text[] NOT NULL DEFAULT '{}',
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            lease_owner varchar(200),
            lease_generation integer NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
            lease_expires_at timestamptz,
            last_error_code varchar(100),
            primary_turn_occurred_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            CHECK (input_manifest IS NULL OR jsonb_typeof(input_manifest) = 'object'),
            CHECK (extraction_result IS NULL OR jsonb_typeof(extraction_result) = 'object'),
            CHECK (merge_plan_result IS NULL OR jsonb_typeof(merge_plan_result) = 'object')
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_knowledge_summary_generation_client_request
        ON conversation.knowledge_summary_generation_jobs (user_id, client_request_id)
        WHERE client_request_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_knowledge_summary_generation_user_processing
        ON conversation.knowledge_summary_generation_jobs (user_id)
        WHERE status = 'processing'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_generation_claimable
        ON conversation.knowledge_summary_generation_jobs
        (status, next_attempt_at, trigger, created_at, generation_id)
        WHERE status IN ('pending', 'retry_wait')
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_generation_turn_current
        ON conversation.knowledge_summary_generation_jobs
        (user_id, thread_id, turn_id, created_at DESC, generation_id DESC)
        """
    )

    op.execute(
        """
        CREATE TABLE conversation.knowledge_summary_model_calls (
            call_id uuid PRIMARY KEY,
            generation_id uuid NOT NULL
                REFERENCES conversation.knowledge_summary_generation_jobs(generation_id)
                ON DELETE CASCADE,
            purpose text NOT NULL CHECK (purpose IN ('extract', 'merge_plan')),
            model_name varchar(100) NOT NULL,
            prompt_version varchar(100) NOT NULL,
            schema_version varchar(50) NOT NULL,
            request_hash char(64) NOT NULL,
            response_payload jsonb,
            input_tokens integer CHECK (input_tokens IS NULL OR input_tokens >= 0),
            output_tokens integer CHECK (output_tokens IS NULL OR output_tokens >= 0),
            latency_ms integer NOT NULL CHECK (latency_ms >= 0),
            status text NOT NULL CHECK (status IN ('succeeded', 'failed')),
            error_code varchar(100),
            payload_scrubbed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CHECK (response_payload IS NULL OR jsonb_typeof(response_payload) = 'object'),
            UNIQUE (generation_id, purpose, request_hash)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_model_calls_generation
        ON conversation.knowledge_summary_model_calls (generation_id, created_at DESC)
        """
    )

    # ------------------------------------------------------------------
    # 消息级来源与不可变 Revision（方案 §7.3 / §7.4）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE conversation.knowledge_summary_sources (
            source_id uuid PRIMARY KEY,
            summary_id uuid NOT NULL REFERENCES conversation.knowledge_summaries(summary_id)
                ON DELETE CASCADE,
            user_id uuid NOT NULL,
            thread_id uuid NOT NULL REFERENCES conversation.conversation_threads(thread_id)
                ON DELETE RESTRICT,
            turn_id uuid NOT NULL REFERENCES conversation.conversation_turns(turn_id)
                ON DELETE RESTRICT,
            message_id uuid NOT NULL REFERENCES conversation.conversation_messages(message_id)
                ON DELETE RESTRICT,
            message_role text NOT NULL CHECK (message_role IN ('user', 'assistant')),
            source_checkpoint_id varchar(500) NOT NULL,
            first_generation_id uuid
                REFERENCES conversation.knowledge_summary_generation_jobs(generation_id)
                ON DELETE SET NULL,
            first_trigger text NOT NULL CHECK (first_trigger IN
                ('auto', 'manual', 'manual_refresh', 'manual_retry', 'ops_retry')),
            status text NOT NULL DEFAULT 'available'
                CHECK (status IN ('available', 'unavailable')),
            message_occurred_at timestamptz NOT NULL,
            message_sequence integer NOT NULL CHECK (message_sequence >= 1),
            created_at timestamptz NOT NULL DEFAULT now(),
            unavailable_at timestamptz,
            UNIQUE (summary_id, message_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_sources_summary_message
        ON conversation.knowledge_summary_sources
        (summary_id, message_occurred_at DESC, message_sequence DESC, source_id DESC)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_sources_thread
        ON conversation.knowledge_summary_sources (thread_id, summary_id)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_sources_user_turn
        ON conversation.knowledge_summary_sources (user_id, turn_id, status)
        """
    )

    op.execute(
        """
        CREATE TABLE conversation.knowledge_summary_revisions (
            revision_id uuid PRIMARY KEY,
            summary_id uuid NOT NULL REFERENCES conversation.knowledge_summaries(summary_id)
                ON DELETE CASCADE,
            user_id uuid NOT NULL,
            version integer NOT NULL CHECK (version >= 1),
            base_version integer NOT NULL CHECK (base_version >= 0),
            mutation_type text NOT NULL CHECK (mutation_type IN
                ('create', 'auto_merge', 'user_edit', 'review_flagged', 'duplicate_flagged',
                 'conflict_resolved', 'duplicate_resolved', 'delete', 'manual_merge')),
            actor_type text NOT NULL CHECK (actor_type IN ('system', 'model', 'user')),
            topic_group_title varchar(160) NOT NULL,
            topic_title varchar(240) NOT NULL,
            content jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'),
            protected_sections text[] NOT NULL DEFAULT '{}',
            content_hash char(64) NOT NULL,
            changed_sections text[] NOT NULL DEFAULT '{}',
            source_ids uuid[] NOT NULL DEFAULT '{}' CHECK (cardinality(source_ids) <= 100),
            generation_id uuid
                REFERENCES conversation.knowledge_summary_generation_jobs(generation_id)
                ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (summary_id, version)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_revisions_summary_version
        ON conversation.knowledge_summary_revisions (summary_id, version DESC)
        """
    )

    # ------------------------------------------------------------------
    # 删除墓碑与旧 Turn 同步抑制索引（方案 §7.7）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE conversation.knowledge_summary_tombstones (
            tombstone_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            deleted_summary_id uuid NOT NULL,
            normalized_topic_group varchar(160) NOT NULL,
            normalized_topic_title varchar(240) NOT NULL,
            normalized_aliases text[] NOT NULL DEFAULT '{}'
                CHECK (cardinality(normalized_aliases) <= 20),
            deleted_at timestamptz NOT NULL,
            latest_source_occurred_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_tombstone_identity
        ON conversation.knowledge_summary_tombstones
        (user_id, normalized_topic_group, normalized_topic_title)
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_tombstone_aliases
        ON conversation.knowledge_summary_tombstones
        USING gin (normalized_aliases)
        """
    )
    op.execute(
        """
        CREATE TABLE conversation.knowledge_summary_tombstone_turns (
            tombstone_id uuid NOT NULL
                REFERENCES conversation.knowledge_summary_tombstones(tombstone_id)
                ON DELETE CASCADE,
            user_id uuid NOT NULL,
            turn_id uuid NOT NULL REFERENCES conversation.conversation_turns(turn_id)
                ON DELETE RESTRICT,
            source_occurred_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tombstone_id, turn_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_tombstone_turn_lookup
        ON conversation.knowledge_summary_tombstone_turns (user_id, turn_id)
        """
    )

    # ------------------------------------------------------------------
    # 冲突、可能重复、运行控制与生产 CLI 审计（方案 §7.8–§7.11）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE conversation.knowledge_summary_reviews (
            review_id uuid PRIMARY KEY,
            generation_id uuid NOT NULL
                REFERENCES conversation.knowledge_summary_generation_jobs(generation_id)
                ON DELETE CASCADE,
            summary_id uuid NOT NULL REFERENCES conversation.knowledge_summaries(summary_id)
                ON DELETE CASCADE,
            user_id uuid NOT NULL,
            candidate_index integer NOT NULL CHECK (candidate_index >= 0),
            reason_code text NOT NULL CHECK (reason_code IN
                ('PROTECTED_SECTION_CONFLICT', 'CONTRADICTORY_CONTENT',
                 'AMBIGUOUS_EXACT_ALIAS', 'UNSAFE_REPLACE', 'STALE_TARGET')),
            internal_reason varchar(300),
            proposed_content jsonb NOT NULL CHECK (jsonb_typeof(proposed_content) = 'object'),
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'dismissed', 'resolved')),
            created_at timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz,
            UNIQUE (generation_id, candidate_index, summary_id, reason_code)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_reviews_pending
        ON conversation.knowledge_summary_reviews (summary_id, created_at DESC)
        WHERE status = 'pending'
        """
    )

    op.execute(
        """
        CREATE TABLE conversation.knowledge_summary_duplicate_candidates (
            duplicate_id uuid PRIMARY KEY,
            generation_id uuid NOT NULL
                REFERENCES conversation.knowledge_summary_generation_jobs(generation_id)
                ON DELETE CASCADE,
            summary_id uuid NOT NULL REFERENCES conversation.knowledge_summaries(summary_id)
                ON DELETE CASCADE,
            possible_target_summary_id uuid NOT NULL
                REFERENCES conversation.knowledge_summaries(summary_id)
                ON DELETE CASCADE,
            user_id uuid NOT NULL,
            match_score numeric(6,5) NOT NULL CHECK (match_score >= 0 AND match_score <= 1),
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'dismissed', 'merged', 'resolved')),
            resolution_reason text CHECK (
                resolution_reason IS NULL
                OR resolution_reason IN ('summary_deleted', 'target_deleted')
            ),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            resolved_at timestamptz,
            CHECK (summary_id <> possible_target_summary_id),
            CHECK (
                (status = 'resolved' AND resolution_reason IS NOT NULL)
                OR (status <> 'resolved' AND resolution_reason IS NULL)
            ),
            CHECK (
                (status = 'pending' AND resolved_at IS NULL)
                OR (status <> 'pending' AND resolved_at IS NOT NULL)
            )
        )
        """
    )
    # 该表达式唯一索引防止 A-B/B-A 重复，但绝不交换两列的业务方向。
    op.execute(
        """
        CREATE UNIQUE INDEX uq_knowledge_summary_duplicate_pair
        ON conversation.knowledge_summary_duplicate_candidates (
            user_id,
            LEAST(summary_id, possible_target_summary_id),
            GREATEST(summary_id, possible_target_summary_id)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_duplicate_pending_summary
        ON conversation.knowledge_summary_duplicate_candidates (summary_id, created_at DESC)
        WHERE status = 'pending'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_duplicate_pending_target
        ON conversation.knowledge_summary_duplicate_candidates
        (possible_target_summary_id, created_at DESC)
        WHERE status = 'pending'
        """
    )

    op.execute(
        """
        CREATE TABLE conversation.knowledge_summary_runtime_control (
            control_key text PRIMARY KEY CHECK (control_key = 'global'),
            auto_generation_suspended boolean NOT NULL DEFAULT false,
            suspend_reason_code text,
            suspend_snapshot jsonb,
            suspended_at timestamptz,
            updated_by text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK (suspend_snapshot IS NULL OR jsonb_typeof(suspend_snapshot) = 'object')
        )
        """
    )
    op.execute(
        """
        INSERT INTO conversation.knowledge_summary_runtime_control
        (control_key, updated_by)
        VALUES ('global', 'migration')
        """
    )

    op.execute(
        """
        CREATE TABLE conversation.knowledge_summary_admin_audit (
            audit_id uuid PRIMARY KEY,
            operator varchar(200) NOT NULL,
            ticket_id varchar(200) NOT NULL,
            command varchar(100) NOT NULL,
            arguments_redacted jsonb NOT NULL CHECK (jsonb_typeof(arguments_redacted) = 'object'),
            affected_row_count integer NOT NULL CHECK (affected_row_count >= 0),
            result text NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_summary_admin_audit_occurred
        ON conversation.knowledge_summary_admin_audit (occurred_at DESC)
        """
    )


def downgrade() -> None:
    """删除知识总结 DDL；保留可能被同库复用的 pg_trgm 扩展。"""
    for table in (
        "conversation.knowledge_summary_admin_audit",
        "conversation.knowledge_summary_runtime_control",
        "conversation.knowledge_summary_duplicate_candidates",
        "conversation.knowledge_summary_reviews",
        "conversation.knowledge_summary_tombstone_turns",
        "conversation.knowledge_summary_tombstones",
        "conversation.knowledge_summary_revisions",
        "conversation.knowledge_summary_sources",
        "conversation.knowledge_summary_model_calls",
        "conversation.knowledge_summary_generation_jobs",
        "conversation.knowledge_summary_aliases",
        "conversation.knowledge_summaries",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute(
        "ALTER TABLE conversation.conversation_turns "
        "DROP COLUMN IF EXISTS knowledge_summary_enqueue_next_attempt_at, "
        "DROP COLUMN IF EXISTS knowledge_summary_enqueue_attempts, "
        "DROP COLUMN IF EXISTS knowledge_summary_enqueue_status"
    )
