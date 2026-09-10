"""memory_operations 证据池批次支持（memory-rebuild §2.6 / §5.2 Phase 0-C）。

证据池复用 ``memory_operations`` 单表（§2.6 决议"方案 A 修订版"），因此本迁移只做
三件事，不新增独立 evidence 表：

1. ``status`` CHECK 放行新状态 ``pending_batch``——证据已提交但未到最短沉淀时长，
   等待 0 点批量入批。共享认领查询只认 ``('queued', 'retry_wait')``，部分索引
   ``ix_memory_operations_claim`` 也带同样谓词，故该状态对 Worker/Gateway
   天然不可见，认领查询与既有索引都不需要改。
2. ``operation_type`` CHECK 放行新类型 ``summarize_user_memory_batch``。
3. 新增可空列 ``batch_operation_id``（自引用 FK）建立"证据 → 批次"归属，
   并补批次扫描与成员回写所需的索引。

这三处都是纯扩展：旧代码在新库上仍可读写，既有 lease/fencing/retry/dead-letter
语义不变（§5.2 Phase 0 验收）。
"""

from __future__ import annotations

from alembic import op

revision = "0007_memory_batch_operations"
down_revision = "0006_global_maintenance_gate"
branch_labels = None
depends_on = None

_OPERATION_STATUSES = (
    "queued",
    "running",
    "retry_wait",
    "succeeded",
    "needs_review",
    "dead_letter",
    "cancelled",
    "pending_batch",
)

_OPERATION_TYPES = (
    "conversation_evidence",
    "activity_evidence",
    "correct_memory",
    "forget_memory",
    "restore_memory",
    "override_learner_profile",
    "review_candidate",
    "set_graph_state",
    "project_summary_to_graph",
    "rebuild_index",
    "verify_checksums",
    "purge_tombstones",
    "cleanup_orphan_versions",
    "cleanup_checkpoints",
    "purge_account_memory",
    "summarize_user_memory_batch",
)


def _drop_single_column_checks(table: str, column: str) -> None:
    """按列定位并删除其单列 CHECK 约束。

    不依赖 PostgreSQL 的自动命名（``<table>_<column>_check``）：0001 迁移用的是
    内联匿名 CHECK，名字由数据库生成，按名字硬编码会在不同建库路径下失配。
    ``conkey`` 精确匹配"恰好只约束该列"的 CHECK，不会误伤其他列的约束。
    """
    op.execute(
        f"""
        DO $$
        DECLARE
            cname text;
        BEGIN
            FOR cname IN
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = '{table}'::regclass
                  AND contype = 'c'
                  AND conkey = ARRAY[
                      (
                          SELECT attnum FROM pg_attribute
                          WHERE attrelid = '{table}'::regclass AND attname = '{column}'
                      )
                  ]
            LOOP
                EXECUTE format('ALTER TABLE {table} DROP CONSTRAINT %I', cname);
            END LOOP;
        END $$
        """
    )


def _set_in_check(table: str, column: str, name: str, values: tuple[str, ...]) -> None:
    allowed = ", ".join(f"'{value}'" for value in values)
    _drop_single_column_checks(table, column)
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({column} IN ({allowed}))")


def upgrade() -> None:
    """放行批次状态与新 operation 类型，并建立批次归属列。"""
    _set_in_check(
        "memory_operations",
        "status",
        "ck_memory_operations_status",
        _OPERATION_STATUSES,
    )
    _set_in_check(
        "memory_operations",
        "operation_type",
        "ck_memory_operations_operation_type",
        _OPERATION_TYPES,
    )

    op.execute(
        """
        ALTER TABLE memory_operations
            ADD COLUMN IF NOT EXISTS batch_operation_id uuid
        """
    )
    # 自引用 FK：证据行的 batch_operation_id 指向该批次的批量 operation。
    # 批量 operation 自身该列为 NULL（它不是任何批次的成员）。
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'fk_memory_operations_batch_operation'
                  AND conrelid = 'memory_operations'::regclass
            ) THEN
                ALTER TABLE memory_operations
                    ADD CONSTRAINT fk_memory_operations_batch_operation
                    FOREIGN KEY (batch_operation_id)
                    REFERENCES memory_operations(operation_id);
            END IF;
        END $$
        """
    )

    # 0 点批量扫描：status='pending_batch' AND next_run_at <= now()，按 user_id 分组。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_operations_pending_batch "
        "ON memory_operations (status, next_run_at, user_id)"
    )
    # 批量终态回写成员：WHERE batch_operation_id = :batch（§2.6 状态机第 3/4 步）。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_operations_batch_operation "
        "ON memory_operations (batch_operation_id) "
        "WHERE batch_operation_id IS NOT NULL"
    )


def downgrade() -> None:
    """收窄约束前先确认没有承载数据的批次行，避免回滚静默丢状态。"""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM memory_operations WHERE status = 'pending_batch'
            ) THEN
                RAISE EXCEPTION
                    '0007_memory_batch_operations 无法回滚：'
                    'memory_operations 已存在 pending_batch 行';
            END IF;
            IF EXISTS (
                SELECT 1 FROM memory_operations
                WHERE operation_type = 'summarize_user_memory_batch'
            ) THEN
                RAISE EXCEPTION
                    '0007_memory_batch_operations 无法回滚：'
                    'memory_operations 已存在 summarize_user_memory_batch 行';
            END IF;
            IF EXISTS (
                SELECT 1 FROM memory_operations WHERE batch_operation_id IS NOT NULL
            ) THEN
                RAISE EXCEPTION
                    '0007_memory_batch_operations 无法回滚：'
                    'memory_operations 已存在批次归属';
            END IF;
        END $$
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_memory_operations_batch_operation")
    op.execute("DROP INDEX IF EXISTS ix_memory_operations_pending_batch")
    op.execute(
        "ALTER TABLE memory_operations "
        "DROP CONSTRAINT IF EXISTS fk_memory_operations_batch_operation"
    )
    op.execute("ALTER TABLE memory_operations DROP COLUMN IF EXISTS batch_operation_id")
    _set_in_check(
        "memory_operations",
        "status",
        "ck_memory_operations_status",
        tuple(s for s in _OPERATION_STATUSES if s != "pending_batch"),
    )
    _set_in_check(
        "memory_operations",
        "operation_type",
        "ck_memory_operations_operation_type",
        tuple(t for t in _OPERATION_TYPES if t != "summarize_user_memory_batch"),
    )
