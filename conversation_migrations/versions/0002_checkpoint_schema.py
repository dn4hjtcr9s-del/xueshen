"""创建 LangGraph Conversation Checkpoint 独立 schema。

Conversation Worker 会把 LangGraph checkpointer 的 search_path 固定到
``conversation_checkpoints``。该 schema 必须先由迁移创建，否则 checkpointer
首次建表时会因没有可用 schema 而启动失败。
"""

from __future__ import annotations

from alembic import op

revision = "0002_checkpoint_schema"
down_revision = "0001_conversation_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """创建 Conversation Checkpoint 专用 schema。"""
    op.execute("CREATE SCHEMA IF NOT EXISTS conversation_checkpoints")


def downgrade() -> None:
    """开发回滚时删除 Checkpoint schema 及其 LangGraph 表。"""
    op.execute("DROP SCHEMA IF EXISTS conversation_checkpoints CASCADE")
