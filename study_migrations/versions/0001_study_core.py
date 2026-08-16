"""创建 Study 域核心 schema（docs/study-plan-push-implementation-plan.md v1.2，§7.1–§7.12）。

独立 study 数据库（public schema），不创建或修改任何 Memory/Auth/Conversation/
Community 表。LangGraph checkpoint 使用独立 schema study_checkpoints（Phase 2 使用），
该 schema 必须先由迁移创建（与 conversation_checkpoints 同模式）。

业务唯一约束与 §7/D20/D21 冻结一致：
- study_plans：每用户最多一个 active 计划（部分唯一索引）；
- study_plan_availability：每计划每个 ISO day_of_week 一行；
- study_daily_feed_runs：UNIQUE(user_id, plan_id, local_date)；
- study_model_call_records：六元组唯一，禁止跨用户复用；
- study_idempotency_requests：UNIQUE(user_id, operation_name, idempotency_key)。
"""

from __future__ import annotations

from alembic import op

revision = "0001_study_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 Study 全部表、索引、约束与独立 checkpoint schema（§7.1–§7.12）。"""
    # LangGraph checkpoint 独立 schema（Phase 2 的 Plan/Feed/Replan Graph 使用；
    # checkpoints/checkpoint_writes/checkpoint_blobs 表由 AsyncPostgresSaver.setup() 创建）
    op.execute("CREATE SCHEMA IF NOT EXISTS study_checkpoints")

    # ------------------------------------------------------------------
    # study_plan_intakes（§7.1）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_plan_intakes (
            intake_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            status text NOT NULL DEFAULT 'collecting'
                CHECK (status IN ('collecting', 'ready', 'confirmed', 'exhausted', 'expired')),
            normalized_intent jsonb,
            missing_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
            recent_messages jsonb NOT NULL DEFAULT '[]'::jsonb,
            message_count integer NOT NULL DEFAULT 0 CHECK (message_count >= 0),
            last_model_call_id uuid,
            version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
            expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_study_plan_intakes_user ON study_plan_intakes "
        "(user_id, created_at DESC)"
    )

    # ------------------------------------------------------------------
    # study_plans（§7.2 / D5 / D25）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_plans (
            plan_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            goal text NOT NULL,
            status text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'active', 'paused', 'completed', 'archived')),
            timezone text NOT NULL,
            start_date date NOT NULL,
            target_date date NOT NULL,
            weekly_minutes integer NOT NULL CHECK (weekly_minutes > 0),
            session_min_minutes integer NOT NULL CHECK (session_min_minutes > 0),
            session_max_minutes integer NOT NULL
                CHECK (session_max_minutes >= session_min_minutes),
            current_revision_id uuid,
            version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
            activated_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT chk_study_plans_target_after_start CHECK (target_date > start_date)
        )
        """
    )
    # §7.2/D5：数据库约束兜底每用户最多一个 active 计划
    op.execute(
        "CREATE UNIQUE INDEX ux_study_plans_one_active ON study_plans "
        "(user_id) WHERE status = 'active'"
    )
    op.execute(
        "CREATE INDEX ix_study_plans_user ON study_plans (user_id, created_at DESC)"
    )

    # ------------------------------------------------------------------
    # study_plan_availability（§7.3 / §8 约束 9/10）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_plan_availability (
            plan_id uuid NOT NULL REFERENCES study_plans(plan_id),
            day_of_week integer NOT NULL CHECK (day_of_week BETWEEN 1 AND 7),
            available_minutes integer NOT NULL DEFAULT 0 CHECK (available_minutes >= 0),
            start_local_time time,
            end_local_time time,
            is_rest_day boolean NOT NULL DEFAULT false,
            PRIMARY KEY (plan_id, day_of_week),
            CONSTRAINT chk_study_availability_rest_day CHECK (
                (is_rest_day AND available_minutes = 0
                 AND start_local_time IS NULL AND end_local_time IS NULL)
                OR (NOT is_rest_day AND available_minutes > 0)
            )
        )
        """
    )

    # ------------------------------------------------------------------
    # study_plan_revisions（§7.4 / D14 / D21）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_plan_revisions (
            revision_id uuid PRIMARY KEY,
            plan_id uuid NOT NULL REFERENCES study_plans(plan_id),
            revision_no integer NOT NULL CHECK (revision_no >= 1),
            reason text NOT NULL
                CHECK (reason IN ('initial', 'user_adjustment', 'weekly_replan',
                                  'missed_task', 'memory_change')),
            status text NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed', 'active', 'rejected', 'superseded')),
            input_snapshot jsonb NOT NULL,
            memory_context_hash text,
            personalization_status text NOT NULL DEFAULT 'not_requested'
                CHECK (personalization_status IN ('personalized', 'degraded', 'not_requested')),
            personalization_reason text,
            proposal_operation_id uuid,
            base_revision_id uuid,
            decision_at timestamptz,
            decision_actor_id uuid,
            decision_reason text,
            model_name text,
            prompt_version text,
            change_summary text,
            created_at timestamptz NOT NULL DEFAULT now(),
            activated_at timestamptz,
            CONSTRAINT ux_study_plan_revisions_no UNIQUE (plan_id, revision_no)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_study_plan_revisions_plan ON study_plan_revisions "
        "(plan_id, revision_no DESC)"
    )

    # ------------------------------------------------------------------
    # study_tasks（§7.5 / D13 / D27）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_tasks (
            task_id uuid PRIMARY KEY,
            plan_id uuid NOT NULL REFERENCES study_plans(plan_id),
            revision_id uuid NOT NULL REFERENCES study_plan_revisions(revision_id),
            scheduled_date date NOT NULL,
            order_index integer NOT NULL DEFAULT 0 CHECK (order_index >= 0),
            task_type text NOT NULL
                CHECK (task_type IN ('learn', 'practice', 'review', 'assessment')),
            title text NOT NULL,
            description text NOT NULL DEFAULT '',
            estimated_minutes integer NOT NULL CHECK (estimated_minutes > 0),
            model_estimated_minutes integer,
            estimation_basis text NOT NULL DEFAULT '',
            topic_key text,
            graph_node_id text,
            reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            source text NOT NULL DEFAULT 'plan'
                CHECK (source IN ('plan', 'recommendation', 'manual')),
            source_feed_item_id uuid,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'in_progress', 'completed', 'skipped', 'cancelled')),
            user_locked boolean NOT NULL DEFAULT false,
            completion_source text
                CHECK (completion_source IS NULL OR completion_source = 'manual'),
            started_at timestamptz,
            completed_at timestamptz,
            version integer NOT NULL DEFAULT 1 CHECK (version >= 1)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_study_tasks_plan_date ON study_tasks "
        "(plan_id, scheduled_date, order_index)"
    )
    op.execute("CREATE INDEX ix_study_tasks_revision ON study_tasks (revision_id)")

    # ------------------------------------------------------------------
    # study_task_events（§7.6）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_task_events (
            event_id uuid PRIMARY KEY,
            task_id uuid NOT NULL REFERENCES study_tasks(task_id),
            event_type text NOT NULL
                CHECK (event_type IN ('created', 'started', 'completion_suggested',
                                      'completed', 'reopened', 'rescheduled',
                                      'skipped', 'cancelled')),
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            revision_id uuid,
            operation_id uuid,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_study_task_events_task ON study_task_events (task_id, created_at)"
    )

    # ------------------------------------------------------------------
    # study_daily_feed_runs（§7.7 / D9 / D20）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_daily_feed_runs (
            feed_run_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            plan_id uuid NOT NULL REFERENCES study_plans(plan_id),
            revision_id uuid,
            local_date date NOT NULL,
            timezone text NOT NULL,
            status text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'stale')),
            operation_id uuid,
            input_hash text,
            generation integer NOT NULL DEFAULT 1 CHECK (generation >= 1),
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            started_at timestamptz,
            completed_at timestamptz,
            last_error_code text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ux_study_daily_feed_runs_key UNIQUE (user_id, plan_id, local_date)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_study_daily_feed_runs_scan ON study_daily_feed_runs "
        "(local_date, status)"
    )

    # ------------------------------------------------------------------
    # study_daily_feed_items（§7.8）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_daily_feed_items (
            feed_item_id uuid PRIMARY KEY,
            feed_run_id uuid NOT NULL REFERENCES study_daily_feed_runs(feed_run_id),
            source_type text NOT NULL,
            task_id uuid,
            topic_key text,
            graph_node_id text,
            title text NOT NULL,
            reason text NOT NULL DEFAULT '',
            reason_codes jsonb NOT NULL DEFAULT '[]'::jsonb,
            estimated_minutes integer,
            launch_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'accepted', 'dismissed', 'expired')),
            expires_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_study_daily_feed_items_run ON study_daily_feed_items (feed_run_id)"
    )

    # ------------------------------------------------------------------
    # study_sessions（§7.9 / D24）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_sessions (
            session_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            task_id uuid REFERENCES study_tasks(task_id),
            conversation_thread_id text,
            conversation_status text NOT NULL DEFAULT 'not_requested'
                CHECK (conversation_status IN ('not_requested', 'pending', 'ready', 'failed')),
            conversation_create_request_id text,
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'completed', 'abandoned')),
            started_at timestamptz NOT NULL DEFAULT now(),
            last_heartbeat_at timestamptz,
            last_heartbeat_seq bigint NOT NULL DEFAULT 0 CHECK (last_heartbeat_seq >= 0),
            ended_at timestamptz,
            active_seconds bigint NOT NULL DEFAULT 0 CHECK (active_seconds >= 0)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_study_sessions_user ON study_sessions (user_id, started_at DESC)"
    )
    op.execute("CREATE INDEX ix_study_sessions_task ON study_sessions (task_id)")

    # ------------------------------------------------------------------
    # study_daily_stats（§7.10）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_daily_stats (
            user_id uuid NOT NULL,
            local_date date NOT NULL,
            active_seconds bigint NOT NULL DEFAULT 0 CHECK (active_seconds >= 0),
            completed_task_count integer NOT NULL DEFAULT 0 CHECK (completed_task_count >= 0),
            session_count integer NOT NULL DEFAULT 0 CHECK (session_count >= 0),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, local_date)
        )
        """
    )

    # ------------------------------------------------------------------
    # study_model_call_records（§7.11 / D15）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_model_call_records (
            model_call_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            operation_id uuid,
            purpose text NOT NULL CHECK (purpose IN ('intake', 'plan', 'feed', 'replan')),
            input_hash char(64) NOT NULL,
            prompt_version text NOT NULL,
            model text NOT NULL,
            schema_version text NOT NULL,
            status text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'succeeded', 'failed')),
            validated_response jsonb,
            error_code text,
            usage jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            CONSTRAINT ux_study_model_call_records_key UNIQUE
                (user_id, purpose, input_hash, prompt_version, model, schema_version)
        )
        """
    )

    # ------------------------------------------------------------------
    # study_operations（§7.12：operation 队列，lease/fencing 语义与 Memory 同模式）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_operations (
            operation_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            operation_type text NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            status text NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'running', 'needs_input',
                                  'succeeded', 'failed', 'cancelled')),
            lease_owner text,
            lease_expires_at timestamptz,
            lease_generation integer NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            max_attempts integer NOT NULL DEFAULT 10 CHECK (max_attempts >= 1),
            result jsonb,
            error_code text,
            error_message text,
            fencing_token uuid NOT NULL DEFAULT gen_random_uuid(),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_study_operations_claim ON study_operations "
        "(status, lease_expires_at, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_study_operations_user ON study_operations "
        "(user_id, created_at DESC)"
    )

    # ------------------------------------------------------------------
    # study_outbox（§7.12 / §15.4：跨域最终一致投递）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_outbox (
            event_id uuid PRIMARY KEY,
            user_id uuid,
            event_type text NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key text,
            status text NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'delivering', 'delivered', 'dead_letter')),
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            max_attempts integer NOT NULL DEFAULT 10 CHECK (max_attempts >= 1),
            lease_owner text,
            lease_expires_at timestamptz,
            last_error text,
            available_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            delivered_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_study_outbox_pending ON study_outbox (status, available_at)"
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_study_outbox_idem ON study_outbox (idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )

    # ------------------------------------------------------------------
    # study_idempotency_requests（§7.12 / D16）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_idempotency_requests (
            idempotency_request_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            operation_name text NOT NULL,
            idempotency_key text NOT NULL,
            request_hash char(64) NOT NULL,
            response_status integer,
            response_body jsonb,
            operation_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            CONSTRAINT ux_study_idempotency_key UNIQUE (user_id, operation_name, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_study_idempotency_expiry ON study_idempotency_requests (expires_at)"
    )

    # ------------------------------------------------------------------
    # study_user_leases（§7.12 / D17：同用户串行的 durable lease）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_user_leases (
            user_id uuid PRIMARY KEY,
            operation_id uuid NOT NULL,
            lease_generation integer NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
            locked_by text NOT NULL,
            lease_expires_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # ------------------------------------------------------------------
    # study_scheduler_runs（§7.12：调度幂等与批量扫描锚点）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_scheduler_runs (
            run_id uuid PRIMARY KEY,
            name text NOT NULL,
            idempotency_key text NOT NULL,
            status text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'succeeded', 'failed')),
            started_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            CONSTRAINT ux_study_scheduler_runs_key UNIQUE (name, idempotency_key)
        )
        """
    )

    # ------------------------------------------------------------------
    # study_account_purge_ledger（§12.8 / D19：删除账本证明清理完成）
    # ------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE study_account_purge_ledger (
            account_deletion_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            status text NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'succeeded', 'failed')),
            started_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            error_message text
        )
        """
    )


def downgrade() -> None:
    """按依赖逆序删除 Study 全部表与 checkpoint schema。"""
    op.execute("DROP TABLE IF EXISTS study_account_purge_ledger")
    op.execute("DROP TABLE IF EXISTS study_scheduler_runs")
    op.execute("DROP TABLE IF EXISTS study_user_leases")
    op.execute("DROP TABLE IF EXISTS study_idempotency_requests")
    op.execute("DROP TABLE IF EXISTS study_outbox")
    op.execute("DROP TABLE IF EXISTS study_operations")
    op.execute("DROP TABLE IF EXISTS study_model_call_records")
    op.execute("DROP TABLE IF EXISTS study_daily_stats")
    op.execute("DROP TABLE IF EXISTS study_sessions")
    op.execute("DROP TABLE IF EXISTS study_daily_feed_items")
    op.execute("DROP TABLE IF EXISTS study_daily_feed_runs")
    op.execute("DROP TABLE IF EXISTS study_task_events")
    op.execute("DROP TABLE IF EXISTS study_tasks")
    op.execute("DROP TABLE IF EXISTS study_plan_revisions")
    op.execute("DROP TABLE IF EXISTS study_plan_availability")
    op.execute("DROP TABLE IF EXISTS study_plans")
    op.execute("DROP TABLE IF EXISTS study_plan_intakes")
    op.execute("DROP SCHEMA IF EXISTS study_checkpoints CASCADE")
