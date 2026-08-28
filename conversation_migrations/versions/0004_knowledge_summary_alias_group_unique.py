"""修复知识总结别名的分组唯一约束（知识总结方案 §12.7）。

同一总结在编辑大主题后，必须同时保留旧大主题下的历史标题 alias 与新大主题下的
当前标题 alias。原 `(summary_id, normalized_alias)` 唯一键无法表达该冻结语义，
因此改为同时包含 `normalized_topic_group`。
"""

from __future__ import annotations

from alembic import op

revision = "0004_ks_alias_group_unique"
down_revision = "0003_knowledge_summaries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """将别名唯一键扩展为 summary、group 和 alias 三元组。"""
    op.execute(
        "ALTER TABLE conversation.knowledge_summary_aliases "
        "DROP CONSTRAINT knowledge_summary_aliases_summary_id_normalized_alias_key"
    )
    op.execute(
        "ALTER TABLE conversation.knowledge_summary_aliases "
        "ADD CONSTRAINT uq_knowledge_summary_alias_group "
        "UNIQUE (summary_id, normalized_topic_group, normalized_alias)"
    )


def downgrade() -> None:
    """回滚为 Phase 0 初始的单总结 alias 唯一语义。"""
    op.execute(
        "ALTER TABLE conversation.knowledge_summary_aliases "
        "DROP CONSTRAINT uq_knowledge_summary_alias_group"
    )
    op.execute(
        "ALTER TABLE conversation.knowledge_summary_aliases "
        "ADD CONSTRAINT knowledge_summary_aliases_summary_id_normalized_alias_key "
        "UNIQUE (summary_id, normalized_alias)"
    )
