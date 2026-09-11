"""memory_operations.operation_type 放行文档 schema 迁移类型（memory-rebuild §5.6 Phase 4）。

Phase 4 在 `contracts/common.py::OperationType` 里加了 ``migrate_markdown_schema_v2``，
但数据库侧的 CHECK 约束是 0007 迁移里用显式清单建立的，**没有同步**——结果是
Scheduler 建维护 operation 时被 ``ck_memory_operations_operation_type`` 拒绝。

这个缺口是 `tests/integration/test_memory_schema_migration.py` 用真实库跑出来的：
Python 的 Literal 与 DB 的 CHECK 是两处独立的真相，只改一处编译期与单测都发现不了。

按"不得修改历史迁移"的规矩，这里新增一条迁移扩展约束，而不是回头改 0007。
"""

from __future__ import annotations

from alembic import op

revision = "0009_memory_migrate_schema_op"
down_revision = "0008_memory_index_v2_columns"
branch_labels = None
depends_on = None

#: 0007 建立的清单 + Phase 4 新增项。顺序无关，保持与 0007 一致便于对照。
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
    "migrate_markdown_schema_v2",
)


def _set_in_check(column: str, name: str, values: tuple[str, ...]) -> None:
    """按列定位并原子替换单列 CHECK（与 0007 同一手法，不依赖 PG 自动命名）。"""
    op.execute(
        """
        DO $$
        DECLARE
            cname text;
        BEGIN
            FOR cname IN
                SELECT conname
                FROM pg_constraint
                WHERE conrelid = 'memory_operations'::regclass
                  AND contype = 'c'
                  AND conkey = ARRAY[
                      (
                          SELECT attnum FROM pg_attribute
                          WHERE attrelid = 'memory_operations'::regclass
                            AND attname = 'operation_type'
                      )
                  ]
            LOOP
                EXECUTE format('ALTER TABLE memory_operations DROP CONSTRAINT %I', cname);
            END LOOP;
        END $$
        """
    )
    allowed = ", ".join(f"'{value}'" for value in values)
    op.execute(
        f"ALTER TABLE memory_operations ADD CONSTRAINT {name} CHECK ({column} IN ({allowed}))"
    )


def upgrade() -> None:
    """放行 ``migrate_markdown_schema_v2``。"""
    _set_in_check("operation_type", "ck_memory_operations_operation_type", _OPERATION_TYPES)


def downgrade() -> None:
    """收窄前先确认没有承载数据的迁移 operation。"""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM memory_operations
                WHERE operation_type = 'migrate_markdown_schema_v2'
            ) THEN
                RAISE EXCEPTION
                    '0009_memory_migrate_schema_op 无法回滚：'
                    'memory_operations 已存在 migrate_markdown_schema_v2 行';
            END IF;
        END $$
        """
    )
    _set_in_check(
        "operation_type",
        "ck_memory_operations_operation_type",
        tuple(t for t in _OPERATION_TYPES if t != "migrate_markdown_schema_v2"),
    )
