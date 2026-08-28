"""修复知识总结模型调用审计的重试记录唯一性（知识总结方案 §7.6）。

同一 Generation 的同一阶段允许多次调用尝试；迁移必须先为历史记录稳定回填序号，
再建立新的唯一约束，确保非空生产数据库可以安全升级。该迁移不可逆，避免回滚时
删除或破坏已保留的重试审计记录。
"""

from __future__ import annotations

from alembic import op

revision = "0005_ks_model_call_attempts"
down_revision = "0004_ks_alias_group_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """按创建时间和调用 ID 为历史模型调用回填 attempt_no 后切换唯一约束。"""
    op.execute(
        "ALTER TABLE conversation.knowledge_summary_model_calls ADD COLUMN attempt_no integer"
    )
    op.execute(
        """
        WITH numbered AS (
            SELECT call_id,
                   ROW_NUMBER() OVER (
                       PARTITION BY generation_id, purpose
                       ORDER BY created_at ASC, call_id ASC
                   ) AS next_attempt_no
            FROM conversation.knowledge_summary_model_calls
        )
        UPDATE conversation.knowledge_summary_model_calls AS calls
        SET attempt_no = numbered.next_attempt_no
        FROM numbered
        WHERE calls.call_id = numbered.call_id
        """
    )
    op.execute(
        "ALTER TABLE conversation.knowledge_summary_model_calls "
        "ALTER COLUMN attempt_no SET DEFAULT 1"
    )
    op.execute(
        "ALTER TABLE conversation.knowledge_summary_model_calls "
        "ALTER COLUMN attempt_no SET NOT NULL"
    )
    op.execute(
        "ALTER TABLE conversation.knowledge_summary_model_calls "
        "ADD CONSTRAINT ck_knowledge_summary_model_call_attempt_no CHECK (attempt_no >= 1)"
    )
    op.execute(
        "ALTER TABLE conversation.knowledge_summary_model_calls "
        "DROP CONSTRAINT knowledge_summary_model_calls_generation_id_purpose_request_key"
    )
    op.execute(
        "ALTER TABLE conversation.knowledge_summary_model_calls "
        "ADD CONSTRAINT uq_knowledge_summary_model_call_attempt "
        "UNIQUE (generation_id, purpose, attempt_no)"
    )


def downgrade() -> None:
    """明确拒绝回滚，避免新审计记录无法映射回旧 request_hash 唯一约束。"""
    raise RuntimeError(
        "0005_ks_model_call_attempts 不可逆：升级后允许同一 request_hash 的多次审计记录，"
        "无法安全恢复旧唯一约束。请保持迁移版本，或先执行经批准的审计归档/数据迁移方案。"
    )
