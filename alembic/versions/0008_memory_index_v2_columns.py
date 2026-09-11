"""memory_index_entries 增加 index v2 投影列（memory-rebuild §5.6 Phase 4 / §3.4）。

index v2 的条目是 ``memory_id | name | description | aliases | keywords | version |
updated_at``。其中 **name 复用既有 ``title``、description 复用既有 ``summary``**
（Phase 4 决策：它们本就是同一份投影，再开两列只会让两边漂移），因此本迁移只新增两列：

- ``aliases``：同一主体的不同叫法，供实体归并与检索召回（§3.2）；
- ``related_topic_keys``：从正文 ``[[link]]`` 解析出的相邻主题，供路由与悬空链接治理
  （§3.2 / §3.4）。

两列都带安全默认值 ``'{}'`` 且 NOT NULL，因此旧代码在新库上读写不受影响；
回填由 Phase 4 的 ``migrate_markdown_schema_v2`` 逐用户任务完成（不在此迁移里做数据变更）。
"""

from __future__ import annotations

from alembic import op

revision = "0008_memory_index_v2_columns"
down_revision = "0007_memory_batch_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """新增 aliases / related_topic_keys 两列（幂等、可重复执行）。"""
    op.execute(
        "ALTER TABLE memory_index_entries "
        "ADD COLUMN IF NOT EXISTS aliases text[] NOT NULL DEFAULT '{}'"
    )
    op.execute(
        "ALTER TABLE memory_index_entries "
        "ADD COLUMN IF NOT EXISTS related_topic_keys text[] NOT NULL DEFAULT '{}'"
    )
    # 按 alias 反查主体（实体归并时用），GIN 索引支持数组包含查询。
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_index_entries_aliases "
        "ON memory_index_entries USING gin (aliases)"
    )


def downgrade() -> None:
    """仅在确认没有依赖新列的写入时回滚（列内数据会一并丢失）。"""
    op.execute("DROP INDEX IF EXISTS ix_memory_index_entries_aliases")
    op.execute("ALTER TABLE memory_index_entries DROP COLUMN IF EXISTS related_topic_keys")
    op.execute("ALTER TABLE memory_index_entries DROP COLUMN IF EXISTS aliases")
