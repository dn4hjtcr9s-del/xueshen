"""Rollout 段的 turn 唯一约束改为"非 deleted 行唯一"（memory-rebuild §5.4 Phase 2）。

Phase 0 建表时用 ``uq_rollout_segment_turn UNIQUE (turn_id)`` 表达"每 turn 一段"，
用于防止重复封存。Phase 2 引入崩溃恢复后该约束过紧：worker 崩溃留下的 ``open`` 段
如果落在一个已经拿不到本地文件的节点上（跨节点重新 claim），既无法续写也无法新建段
——因为 turn_id 已被占用。

处理方式（Phase 2 决策）：跨节点时把旧 ``open`` 段标为 ``deleted``（保留审计痕迹），
再为同一 turn 新建段。因此唯一性只需对**非 deleted 行**成立，改为部分唯一索引。

注意 ORDER：必须先建部分唯一索引再删旧约束，否则中间存在窗口；这里在同一事务内完成
（Alembic 默认 transactional DDL），窗口不可观测。
"""

from __future__ import annotations

from alembic import op

revision = "0008_rollout_segment_turn_uq"
down_revision = "0007_rollout_manifest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """把 turn 级唯一约束放宽为"仅对非 deleted 行唯一"。"""
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rollout_segment_turn_active "
        "ON conversation.conversation_rollout_segments (turn_id) "
        "WHERE status <> 'deleted'"
    )
    op.execute(
        "ALTER TABLE conversation.conversation_rollout_segments "
        "DROP CONSTRAINT IF EXISTS uq_rollout_segment_turn"
    )


def downgrade() -> None:
    """回滚前确认不存在"同一 turn 多个非 deleted 段"，否则新约束建不起来。"""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT turn_id
                FROM conversation.conversation_rollout_segments
                WHERE status <> 'deleted'
                GROUP BY turn_id
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    '0008_rollout_segment_turn_uq 无法回滚：'
                    '存在同一 turn 的多个非 deleted 段（跨节点重建留下的痕迹）';
            END IF;
        END $$
        """
    )
    op.execute(
        "ALTER TABLE conversation.conversation_rollout_segments "
        "ADD CONSTRAINT uq_rollout_segment_turn UNIQUE (turn_id)"
    )
    op.execute("DROP INDEX IF EXISTS conversation.uq_rollout_segment_turn_active")
