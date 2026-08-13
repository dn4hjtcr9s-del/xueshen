"""创建 Conversation 域核心 schema（方案 §7）：线程、消息、Turn、事件、Outbox、摘要、Job。

独立 conversation 数据库，不创建或修改任何 Memory/RAG/Auth 表。
LangGraph Conversation Checkpoint 也放本库独立 schema（conversation_checkpoints，Phase 2 建）。
"""

from __future__ import annotations

from alembic import op

revision = "0001_conversation_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 Conversation 全部表与约束（§7.1–§7.7）。"""
    op.execute("CREATE SCHEMA IF NOT EXISTS conversation")
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    # ------------------------------------------------------------------
    # conversation_threads（§7.1）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE conversation.conversation_threads (
            thread_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            title varchar(240),
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'archived', 'deleting', 'deleted')),
            version integer NOT NULL DEFAULT 0 CHECK (version >= 0),
            last_message_sequence integer NOT NULL DEFAULT 0 CHECK (last_message_sequence >= 0),
            deletion_generation integer NOT NULL DEFAULT 0 CHECK (deletion_generation >= 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_conv_threads_user_updated ON conversation.conversation_threads "
        "(user_id, updated_at DESC, thread_id DESC)"
    )

    # ------------------------------------------------------------------
    # conversation_messages（§7.3）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE conversation.conversation_messages (
            message_id uuid PRIMARY KEY,
            thread_id uuid NOT NULL REFERENCES conversation.conversation_threads(thread_id),
            turn_id uuid NOT NULL,
            user_id uuid NOT NULL,
            sequence integer NOT NULL CHECK (sequence >= 1),
            role text NOT NULL CHECK (role IN ('user', 'assistant')),
            content text NOT NULL,
            status text NOT NULL DEFAULT 'completed'
                CHECK (status IN ('completed', 'cancelled', 'failed', 'deleted')),
            content_hash char(64) NOT NULL,
            eligible_for_context boolean NOT NULL DEFAULT true,
            eligible_for_memory boolean NOT NULL DEFAULT true,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            deleted_at timestamptz,
            UNIQUE (thread_id, sequence)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_conv_messages_thread_seq ON conversation.conversation_messages "
        "(thread_id, sequence DESC)"
    )

    # ------------------------------------------------------------------
    # conversation_turns（§7.2）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE conversation.conversation_turns (
            turn_id uuid PRIMARY KEY,
            thread_id uuid NOT NULL REFERENCES conversation.conversation_threads(thread_id),
            user_id uuid NOT NULL,
            client_request_id varchar(200) NOT NULL,
            request_id varchar(200) NOT NULL,
            run_id varchar(200) NOT NULL,
            user_message_id uuid NOT NULL,
            assistant_message_id uuid,
            status text NOT NULL DEFAULT 'accepted'
                CHECK (status IN
                       ('accepted', 'running', 'cancelling', 'completed', 'failed', 'cancelled')),
            lease_owner varchar(200),
            lease_generation integer NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
            lease_expires_at timestamptz,
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at timestamptz NOT NULL,
            expected_thread_version integer NOT NULL CHECK (expected_thread_version >= 0),
            graph_thread_id varchar(200),
            graph_checkpoint_id varchar(200),
            source_checkpoint_id varchar(500),
            plan_revision integer NOT NULL DEFAULT 0 CHECK (plan_revision >= 0),
            memory_trigger text NOT NULL DEFAULT 'turn_boundary'
                CHECK (memory_trigger IN ('turn_boundary', 'explicit_remember')),
            memory_submission_status text NOT NULL DEFAULT 'not_required'
                CHECK (memory_submission_status IN
                       ('not_required', 'pending', 'retrying', 'accepted', 'failed')),
            memory_operation_id uuid,
            last_event_sequence integer NOT NULL DEFAULT 0 CHECK (last_event_sequence >= 0),
            degraded_flags text[] NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (thread_id, client_request_id)
        )
        """
    )
    # 同一 thread 默认最多一个活动 Turn（§5.4 业务串行约束）
    op.execute(
        "CREATE UNIQUE INDEX uq_conv_turns_one_active_per_thread "
        "ON conversation.conversation_turns (thread_id) "
        "WHERE status IN ('accepted', 'running', 'cancelling')"
    )
    op.execute(
        "CREATE INDEX ix_conv_turns_claimable ON conversation.conversation_turns "
        "(status, next_attempt_at) WHERE status = 'accepted'"
    )
    op.execute(
        "CREATE INDEX ix_conv_turns_expired_lease ON conversation.conversation_turns "
        "(status, lease_expires_at) WHERE status IN ('running', 'cancelling')"
    )

    # ------------------------------------------------------------------
    # conversation_turn_events（§7.4）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE conversation.conversation_turn_events (
            event_id uuid PRIMARY KEY,
            turn_id uuid NOT NULL REFERENCES conversation.conversation_turns(turn_id)
                ON DELETE CASCADE,
            sequence integer NOT NULL CHECK (sequence >= 1),
            event_type text NOT NULL CHECK (event_type IN (
                'turn.accepted', 'turn.started', 'answer.delta', 'citation.available',
                'turn.degraded', 'memory.submission', 'answer.completed',
                'turn.failed', 'turn.cancelled')),
            request_id varchar(200) NOT NULL,
            run_id varchar(200) NOT NULL,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            UNIQUE (turn_id, sequence)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_conv_events_turn_seq ON conversation.conversation_turn_events "
        "(turn_id, sequence)"
    )

    # ------------------------------------------------------------------
    # conversation_outbox（§7.5）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE conversation.conversation_outbox (
            event_id uuid PRIMARY KEY,
            event_type text NOT NULL CHECK (event_type IN
                ('conversation_evidence', 'memory.source_deleted')),
            aggregate_type text NOT NULL,
            aggregate_id text NOT NULL,
            aggregate_version integer NOT NULL CHECK (aggregate_version >= 1),
            idempotency_key varchar(500) NOT NULL,
            user_id uuid NOT NULL,
            thread_id uuid NOT NULL,
            turn_id uuid,
            message_ids uuid[] NOT NULL DEFAULT '{}',
            source_checkpoint_id varchar(500),
            trigger text,
            topic_hints text[] NOT NULL DEFAULT '{}',
            graph_node_hints text[] NOT NULL DEFAULT '{}',
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN
                       ('pending', 'processing', 'retry_wait', 'delivered', 'dead_letter')),
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            lease_owner varchar(200),
            lease_generation integer NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
            lease_expires_at timestamptz,
            last_error_code varchar(100),
            created_at timestamptz NOT NULL DEFAULT now(),
            delivered_at timestamptz,
            UNIQUE (idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_conv_outbox_claimable ON conversation.conversation_outbox "
        "(status, next_attempt_at) WHERE status IN ('pending', 'retry_wait')"
    )

    # ------------------------------------------------------------------
    # conversation_summaries（§7.6）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE conversation.conversation_summaries (
            thread_id uuid NOT NULL REFERENCES conversation.conversation_threads(thread_id),
            sequence integer NOT NULL CHECK (sequence >= 1),
            content text NOT NULL,
            token_count integer NOT NULL CHECK (token_count >= 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (thread_id, sequence)
        )
        """
    )

    # ------------------------------------------------------------------
    # conversation_jobs（§7.7）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE conversation.conversation_jobs (
            job_id uuid PRIMARY KEY,
            job_type text NOT NULL CHECK (job_type IN
                ('generate_title', 'summarize_thread', 'delete_thread')),
            thread_id uuid NOT NULL REFERENCES conversation.conversation_threads(thread_id),
            user_id uuid NOT NULL,
            target_sequence integer,
            deletion_generation integer,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'retry_wait', 'done', 'dead_letter')),
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            lease_owner varchar(200),
            lease_generation integer NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
            lease_expires_at timestamptz,
            last_error_code varchar(100),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (job_type, thread_id, target_sequence)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_conv_jobs_delete_generation "
        "ON conversation.conversation_jobs (job_type, thread_id, deletion_generation) "
        "WHERE job_type = 'delete_thread' AND deletion_generation IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_conv_jobs_claimable ON conversation.conversation_jobs "
        "(status, next_attempt_at) WHERE status IN ('pending', 'retry_wait')"
    )


def downgrade() -> None:
    """逆序删除 Conversation 表（不回滚业务数据，仅用于开发）。"""
    for table in (
        "conversation.conversation_jobs",
        "conversation.conversation_summaries",
        "conversation.conversation_outbox",
        "conversation.conversation_turn_events",
        "conversation.conversation_turns",
        "conversation.conversation_messages",
        "conversation.conversation_threads",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
