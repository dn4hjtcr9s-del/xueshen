"""0001 memory core DDL（规格 §13 全部表、扩展、索引与 CHECK 约束）

Revision ID: 0001_memory_core
Revises:
Create Date: 2026-08-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_memory_core"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # §13.1 扩展和身份映射
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute(
        """
        CREATE TABLE account_identity_mappings (
            internal_user_id uuid NOT NULL,
            issuer varchar(300) NOT NULL,
            external_subject varchar(300) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (internal_user_id, issuer),
            UNIQUE (issuer, external_subject)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_identity_mapping_internal_user "
        "ON account_identity_mappings (internal_user_id)"
    )

    # §13.2 memory_operations
    op.execute(
        """
        CREATE TABLE memory_operations (
            operation_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            actor_type text NOT NULL CHECK (actor_type IN (
                'user', 'conversation_agent', 'activity_agent',
                'knowledge_graph_ui', 'summary_projection', 'system', 'admin'
            )),
            input_kind text NOT NULL CHECK (input_kind IN (
                'evidence', 'command', 'projection', 'maintenance'
            )),
            operation_type text NOT NULL CHECK (operation_type IN (
                'conversation_evidence', 'activity_evidence',
                'correct_memory', 'forget_memory', 'restore_memory',
                'override_learner_profile', 'review_candidate',
                'set_graph_state', 'project_summary_to_graph',
                'rebuild_index', 'verify_checksums', 'purge_tombstones',
                'cleanup_orphan_versions', 'cleanup_checkpoints',
                'purge_account_memory'
            )),
            idempotency_key varchar(200) NOT NULL,
            idempotency_payload_hash char(64) NOT NULL,
            priority smallint NOT NULL CHECK (priority BETWEEN 0 AND 100),
            status text NOT NULL CHECK (status IN (
                'queued', 'running', 'retry_wait', 'succeeded',
                'needs_review', 'dead_letter', 'cancelled'
            )),
            payload jsonb NOT NULL,
            result jsonb,
            public_error jsonb,
            trace_id varchar(64) NOT NULL,
            graph_thread_id varchar(128) NOT NULL,
            occurred_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            completed_at timestamptz,
            next_run_at timestamptz NOT NULL DEFAULT now(),
            attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            max_attempts integer NOT NULL CHECK (max_attempts BETWEEN 1 AND 20),
            locked_by varchar(128),
            lease_expires_at timestamptz,
            last_heartbeat_at timestamptz,
            llm_call_count integer NOT NULL DEFAULT 0 CHECK (llm_call_count >= 0),
            cancel_requested_at timestamptz,
            CONSTRAINT uq_memory_operation_idempotency
                UNIQUE (user_id, actor_type, idempotency_key)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_memory_operations_claim "
        "ON memory_operations (priority DESC, created_at ASC) "
        "WHERE status IN ('queued', 'retry_wait')"
    )
    op.execute(
        "CREATE INDEX ix_memory_operations_user_created "
        "ON memory_operations (user_id, created_at DESC)"
    )
    op.execute(
        "CREATE INDEX ix_memory_operations_lease "
        "ON memory_operations (lease_expires_at) WHERE status = 'running'"
    )

    # §13.3 memory_documents
    op.execute(
        """
        CREATE TABLE memory_documents (
            user_id uuid NOT NULL,
            memory_id varchar(160) NOT NULL,
            memory_type text NOT NULL CHECK (memory_type IN ('index', 'learner', 'mastery')),
            topic_key varchar(160),
            topic_title varchar(240),
            logical_path varchar(500) NOT NULL,
            active_version bigint,
            active_storage_key varchar(1000),
            active_checksum char(64),
            deleted_version bigint,
            index_dirty_at timestamptz,
            deleted_at timestamptz,
            tombstone_until timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, memory_id),
            CONSTRAINT uq_memory_document_path UNIQUE (user_id, logical_path),
            CONSTRAINT ck_memory_document_topic CHECK (
                (memory_type = 'mastery' AND topic_key IS NOT NULL AND topic_title IS NOT NULL)
                OR
                (memory_type IN ('index', 'learner') AND topic_key IS NULL)
            ),
            CONSTRAINT ck_memory_document_deleted_state CHECK (
                deleted_at IS NULL
                OR (
                    active_version IS NULL
                    AND active_storage_key IS NULL
                    AND active_checksum IS NULL
                    AND deleted_version IS NOT NULL
                    AND tombstone_until IS NOT NULL
                )
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_memory_mastery_topic "
        "ON memory_documents (user_id, topic_key) WHERE memory_type = 'mastery'"
    )
    op.execute(
        "CREATE INDEX ix_memory_documents_active "
        "ON memory_documents (user_id, updated_at DESC) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_memory_documents_tombstone "
        "ON memory_documents (tombstone_until) WHERE deleted_at IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_memory_documents_index_dirty "
        "ON memory_documents (index_dirty_at) "
        "WHERE memory_type = 'index' AND index_dirty_at IS NOT NULL"
    )

    # §13.4 memory_commits
    op.execute(
        """
        CREATE TABLE memory_commits (
            commit_id uuid PRIMARY KEY,
            mutation_id uuid NOT NULL UNIQUE,
            operation_id uuid NOT NULL REFERENCES memory_operations(operation_id),
            user_id uuid NOT NULL,
            memory_id varchar(160) NOT NULL,
            action text NOT NULL CHECK (action IN (
                'create', 'merge', 'replace', 'append_evidence',
                'forget', 'restore', 'rebuild_index'
            )),
            before_version bigint,
            after_version bigint,
            storage_key varchar(1000),
            checksum char(64),
            actor_type text NOT NULL,
            evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
                jsonb_typeof(evidence_refs) = 'array'
                AND jsonb_array_length(evidence_refs) <= 100
            ),
            commit_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            prompt_version varchar(100),
            model_name varchar(100),
            created_at timestamptz NOT NULL DEFAULT now(),
            FOREIGN KEY (user_id, memory_id)
                REFERENCES memory_documents(user_id, memory_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_memory_commits_document "
        "ON memory_commits (user_id, memory_id, created_at DESC)"
    )
    op.execute("CREATE INDEX ix_memory_commits_operation ON memory_commits (operation_id)")

    # §13.5 memory_index_entries
    op.execute(
        """
        CREATE TABLE memory_index_entries (
            user_id uuid NOT NULL,
            memory_id varchar(160) NOT NULL,
            source_version bigint NOT NULL,
            memory_type text NOT NULL CHECK (memory_type IN ('learner', 'mastery')),
            topic_key varchar(160),
            title varchar(240) NOT NULL,
            summary text NOT NULL,
            keywords text[] NOT NULL DEFAULT '{}',
            search_text text NOT NULL,
            evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
                jsonb_typeof(evidence_refs) = 'array'
                AND jsonb_array_length(evidence_refs) <= 100
            ),
            confidence real,
            updated_at timestamptz NOT NULL,
            PRIMARY KEY (user_id, memory_id),
            FOREIGN KEY (user_id, memory_id)
                REFERENCES memory_documents(user_id, memory_id)
                ON DELETE CASCADE
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_memory_index_search_trgm "
        "ON memory_index_entries USING gin (search_text gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_memory_index_title_trgm "
        "ON memory_index_entries USING gin (title gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_memory_index_keywords ON memory_index_entries USING gin (keywords)"
    )

    # §13.6 memory_review_candidates
    op.execute(
        """
        CREATE TABLE memory_review_candidates (
            candidate_id uuid PRIMARY KEY,
            operation_id uuid NOT NULL REFERENCES memory_operations(operation_id),
            user_id uuid NOT NULL,
            candidate_type text NOT NULL CHECK (candidate_type IN (
                'learner', 'mastery', 'topic_conflict', 'version_conflict'
            )),
            base_memory_id varchar(160),
            base_version bigint,
            topic_key varchar(160),
            normalized_match_key varchar(300) NOT NULL,
            resolution_target text CHECK (resolution_target IN (
                'merge_existing', 'create_new_topic'
            )),
            target_memory_id varchar(160),
            resolved_operation_id uuid REFERENCES memory_operations(operation_id),
            candidate_payload jsonb NOT NULL,
            evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
                jsonb_typeof(evidence_refs) = 'array'
                AND jsonb_array_length(evidence_refs) <= 100
            ),
            confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
            status text NOT NULL CHECK (status IN (
                'pending', 'accepted', 'corrected', 'rejected', 'expired'
            )),
            reviewed_by uuid,
            reviewed_at timestamptz,
            tombstone_until timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_memory_review_user_pending "
        "ON memory_review_candidates (user_id, created_at DESC) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE INDEX ix_memory_review_match_key "
        "ON memory_review_candidates (user_id, normalized_match_key, created_at DESC)"
    )

    # §13.7 记忆删除抑制
    op.execute(
        """
        CREATE TABLE memory_deleted_evidence_suppressions (
            user_id uuid NOT NULL,
            memory_id varchar(160) NOT NULL,
            evidence_ref_hash char(64) NOT NULL,
            hash_key_version varchar(32) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, memory_id, evidence_ref_hash)
        )
        """
    )

    # §13.8 知识图谱只读注册表
    op.execute(
        """
        CREATE TABLE knowledge_graph_nodes (
            node_id varchar(16) PRIMARY KEY CHECK (node_id ~ '^n[0-9]{3,}$'),
            title varchar(300) NOT NULL,
            group_key varchar(100),
            source_file varchar(300) NOT NULL,
            source_checksum char(64) NOT NULL,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            synced_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE knowledge_graph_edges (
            from_node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id),
            to_node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id),
            relation_type text NOT NULL DEFAULT 'prerequisite',
            source_checksum char(64) NOT NULL,
            PRIMARY KEY (from_node_id, to_node_id, relation_type)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE knowledge_graph_sync_runs (
            run_id uuid PRIMARY KEY,
            graph_file varchar(300) NOT NULL,
            graph_checksum char(64) NOT NULL,
            catalog_file varchar(300) NOT NULL,
            catalog_checksum char(64) NOT NULL,
            manifest_checksum char(64) NOT NULL,
            applied boolean NOT NULL DEFAULT false,
            result jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            applied_at timestamptz
        )
        """
    )
    op.execute(
        """
        CREATE TABLE knowledge_graph_node_aliases (
            node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id)
                ON DELETE CASCADE,
            alias varchar(300) NOT NULL,
            normalized_alias varchar(300) NOT NULL,
            alias_source text NOT NULL CHECK (alias_source IN (
                'repository', 'manual_curated', 'derived'
            )),
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (node_id, normalized_alias)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_knowledge_graph_alias_lookup "
        "ON knowledge_graph_node_aliases (normalized_alias)"
    )
    op.execute(
        """
        CREATE TABLE knowledge_graph_node_removal_audit (
            removal_audit_id uuid PRIMARY KEY,
            sync_run_id uuid NOT NULL REFERENCES knowledge_graph_sync_runs(run_id),
            node_id varchar(16) NOT NULL,
            record_type text NOT NULL CHECK (record_type IN (
                'graph_user_states', 'graph_user_node_activity', 'memory_graph_links'
            )),
            user_hash char(64),
            original_record_checksum char(64) NOT NULL,
            affected_count integer NOT NULL CHECK (affected_count >= 0),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # §13.8.1 memory_graph_links
    op.execute(
        """
        CREATE TABLE memory_graph_links (
            user_id uuid NOT NULL,
            memory_id varchar(160) NOT NULL,
            node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id),
            memory_version bigint NOT NULL CHECK (memory_version >= 1),
            mapping_method text NOT NULL CHECK (mapping_method IN (
                'explicit_hint', 'exact_alias', 'model_candidate'
            )),
            mapping_confidence real NOT NULL CHECK (mapping_confidence BETWEEN 0 AND 1),
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, memory_id, node_id),
            FOREIGN KEY (user_id, memory_id)
                REFERENCES memory_documents(user_id, memory_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_memory_graph_links_active_node "
        "ON memory_graph_links (user_id, node_id, memory_version) WHERE active = true"
    )
    op.execute(
        "CREATE INDEX ix_memory_graph_links_active_memory "
        "ON memory_graph_links (user_id, memory_id, memory_version) WHERE active = true"
    )

    # §13.9 graph_user_states / activity
    op.execute(
        """
        CREATE TABLE graph_user_states (
            user_id uuid NOT NULL,
            node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id),
            status text NOT NULL CHECK (status IN ('learning', 'proficient', 'expert')),
            version bigint NOT NULL DEFAULT 1 CHECK (version >= 1),
            status_source text NOT NULL CHECK (status_source IN (
                'user', 'summary_memory', 'system_recompute'
            )),
            source_memory_id varchar(160),
            source_memory_version bigint,
            evidence_snapshot jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
                jsonb_typeof(evidence_snapshot) = 'array'
                AND jsonb_array_length(evidence_snapshot) <= 50
            ),
            evidence_count integer NOT NULL DEFAULT 0 CHECK (evidence_count >= 0),
            last_user_action_at timestamptz,
            last_evidence_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, node_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_graph_user_states_status "
        "ON graph_user_states (user_id, status, updated_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE graph_user_node_activity (
            user_id uuid NOT NULL,
            node_id varchar(16) NOT NULL REFERENCES knowledge_graph_nodes(node_id),
            last_viewed_at timestamptz,
            last_bookmarked_at timestamptz,
            last_check_in_at timestamptz,
            event_count integer NOT NULL DEFAULT 0 CHECK (event_count >= 0),
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, node_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_graph_user_node_activity_recommendation "
        "ON graph_user_node_activity (user_id, updated_at DESC)"
    )

    # §13.10 graph_state_audit
    op.execute(
        """
        CREATE TABLE graph_state_audit (
            audit_id uuid PRIMARY KEY,
            operation_id uuid REFERENCES memory_operations(operation_id),
            user_id uuid NOT NULL,
            node_id varchar(16) NOT NULL,
            before_status text,
            after_status text,
            before_version bigint,
            after_version bigint,
            actor_type text NOT NULL,
            reason_codes text[] NOT NULL DEFAULT '{}',
            evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb CHECK (
                jsonb_typeof(evidence_refs) = 'array'
                AND jsonb_array_length(evidence_refs) <= 50
            ),
            explanation_summary varchar(500),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_graph_state_audit_user_node "
        "ON graph_state_audit (user_id, node_id, created_at DESC)"
    )

    # §13.10.1 source_deletions
    op.execute(
        """
        CREATE TABLE source_deletions (
            source_deletion_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            source_system text NOT NULL CHECK (source_system IN ('conversation', 'activity')),
            source_ref varchar(500) NOT NULL,
            source_version varchar(200),
            deleted_at timestamptz NOT NULL,
            idempotency_hash char(64) NOT NULL UNIQUE,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_source_deletions_lookup "
        "ON source_deletions (user_id, source_system, source_ref, source_version)"
    )

    # §13.11 memory_outbox
    op.execute(
        """
        CREATE TABLE memory_outbox (
            outbox_id uuid PRIMARY KEY,
            operation_id uuid REFERENCES memory_operations(operation_id),
            user_id uuid NOT NULL,
            event_type text NOT NULL CHECK (event_type IN (
                'memory.changed',
                'memory.deleted',
                'memory.restored',
                'learner.updated',
                'review_candidate.created',
                'review_candidate.resolved',
                'graph_state.changed',
                'graph_state.explanation_available',
                'account_memory.purge_requested'
            )),
            aggregate_type text NOT NULL,
            aggregate_id varchar(200) NOT NULL,
            aggregate_version bigint NOT NULL DEFAULT 0 CHECK (aggregate_version >= 0),
            payload jsonb NOT NULL,
            status text NOT NULL DEFAULT 'pending' CHECK (status IN (
                'pending', 'publishing', 'published', 'retry_wait', 'dead_letter'
            )),
            attempt_count integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 10,
            next_run_at timestamptz NOT NULL DEFAULT now(),
            locked_by varchar(128),
            lease_expires_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            published_at timestamptz,
            last_error jsonb,
            CONSTRAINT uq_memory_outbox_event
                UNIQUE (event_type, aggregate_type, aggregate_id, aggregate_version)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_memory_outbox_claim "
        "ON memory_outbox (created_at ASC) WHERE status IN ('pending', 'retry_wait')"
    )

    # §13.12 deliveries / internal event log
    op.execute(
        """
        CREATE TABLE memory_outbox_deliveries (
            delivery_id uuid PRIMARY KEY,
            outbox_id uuid NOT NULL REFERENCES memory_outbox(outbox_id) ON DELETE CASCADE,
            target text NOT NULL CHECK (target IN (
                'summary_projection',
                'user_notification',
                'internal_event_log'
            )),
            status text NOT NULL DEFAULT 'pending' CHECK (status IN (
                'pending', 'succeeded', 'retry_wait', 'dead_letter'
            )),
            idempotency_key varchar(300) NOT NULL,
            attempt_count integer NOT NULL DEFAULT 0,
            last_error jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            UNIQUE (outbox_id, target),
            UNIQUE (target, idempotency_key)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE memory_internal_event_log (
            event_log_id uuid PRIMARY KEY,
            outbox_id uuid NOT NULL REFERENCES memory_outbox(outbox_id) ON DELETE CASCADE,
            event_type varchar(100) NOT NULL,
            idempotency_key varchar(300) NOT NULL UNIQUE,
            user_id uuid NOT NULL,
            payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # §13.13 用户通知
    op.execute(
        """
        CREATE TABLE memory_user_notifications (
            notification_id uuid PRIMARY KEY,
            user_id uuid NOT NULL,
            event_type text NOT NULL,
            title varchar(200) NOT NULL,
            body varchar(1000) NOT NULL,
            aggregate_type varchar(100) NOT NULL,
            aggregate_id varchar(200) NOT NULL,
            source_outbox_id uuid NOT NULL REFERENCES memory_outbox(outbox_id),
            read_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (source_outbox_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_memory_notifications_user "
        "ON memory_user_notifications (user_id, created_at DESC)"
    )

    # §13.14 备份、维护和模型指标
    op.execute(
        """
        CREATE TABLE backup_runs (
            batch_id uuid PRIMARY KEY,
            status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
            backup_root varchar(1000) NOT NULL,
            postgres_artifact varchar(1000) NOT NULL,
            markdown_artifact varchar(1000) NOT NULL,
            manifest_artifact varchar(1000) NOT NULL,
            postgres_checksum char(64),
            markdown_checksum char(64),
            manifest_checksum char(64),
            restore_verification_status text NOT NULL DEFAULT 'pending' CHECK (
                restore_verification_status IN ('pending', 'succeeded', 'failed')
            ),
            restore_verified_at timestamptz,
            restore_verification_error varchar(1000),
            started_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            error_summary varchar(1000)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_backup_runs_status_started "
        "ON backup_runs (status, started_at DESC)"
    )
    op.execute(
        """
        CREATE TABLE memory_maintenance_runs (
            run_id uuid PRIMARY KEY,
            operation_id uuid REFERENCES memory_operations(operation_id),
            maintenance_type text NOT NULL,
            idempotency_key varchar(200) NOT NULL UNIQUE,
            status text NOT NULL CHECK (status IN (
                'queued', 'running', 'succeeded', 'failed'
            )),
            cursor varchar(500),
            result jsonb,
            started_at timestamptz,
            completed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE memory_llm_call_metrics (
            call_id uuid PRIMARY KEY,
            operation_id uuid NOT NULL REFERENCES memory_operations(operation_id),
            user_hash char(64) NOT NULL,
            model_name varchar(100) NOT NULL,
            prompt_version varchar(100) NOT NULL,
            schema_name varchar(100) NOT NULL,
            status text NOT NULL,
            input_tokens integer,
            output_tokens integer,
            latency_ms integer,
            error_code varchar(100),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # §13.15 break-glass
    op.execute(
        """
        CREATE TABLE memory_break_glass_grants (
            grant_id uuid PRIMARY KEY,
            admin_user_id uuid NOT NULL,
            target_user_id uuid NOT NULL,
            reason varchar(500) NOT NULL,
            scopes text[] NOT NULL,
            approved_by uuid,
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE memory_break_glass_audit (
            audit_id uuid PRIMARY KEY,
            grant_id uuid NOT NULL REFERENCES memory_break_glass_grants(grant_id),
            admin_user_id uuid NOT NULL,
            target_user_id uuid NOT NULL,
            action varchar(100) NOT NULL,
            resource_type varchar(100) NOT NULL,
            resource_id varchar(300),
            trace_id varchar(64) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # §13.16 账号删除 manifest 与隐私审计
    op.execute(
        """
        CREATE TABLE account_deletion_manifest (
            account_deletion_id uuid PRIMARY KEY,
            user_hash char(64) NOT NULL UNIQUE,
            user_hash_key_version varchar(32) NOT NULL,
            status text NOT NULL CHECK (status IN (
                'requested', 'running', 'completed', 'failed'
            )),
            requested_at timestamptz NOT NULL,
            purge_completed_at timestamptz,
            backup_retention_until timestamptz NOT NULL,
            completion_proof_checksum char(64),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE memory_privacy_audit_records (
            privacy_audit_id uuid PRIMARY KEY,
            user_hash char(64) NOT NULL,
            user_hash_key_version varchar(32) NOT NULL,
            action varchar(100) NOT NULL,
            actor_hash char(64),
            occurred_at timestamptz NOT NULL,
            proof_checksum char(64) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_memory_privacy_audit_user_time "
        "ON memory_privacy_audit_records (user_hash, occurred_at DESC)"
    )


def downgrade() -> None:
    for table in [
        "memory_privacy_audit_records",
        "account_deletion_manifest",
        "memory_break_glass_audit",
        "memory_break_glass_grants",
        "memory_llm_call_metrics",
        "memory_maintenance_runs",
        "backup_runs",
        "memory_user_notifications",
        "memory_internal_event_log",
        "memory_outbox_deliveries",
        "memory_outbox",
        "source_deletions",
        "graph_state_audit",
        "graph_user_node_activity",
        "graph_user_states",
        "memory_graph_links",
        "knowledge_graph_node_removal_audit",
        "knowledge_graph_node_aliases",
        "knowledge_graph_sync_runs",
        "knowledge_graph_edges",
        "knowledge_graph_nodes",
        "memory_deleted_evidence_suppressions",
        "memory_review_candidates",
        "memory_index_entries",
        "memory_commits",
        "memory_documents",
        "memory_operations",
        "account_identity_mappings",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {table}")
