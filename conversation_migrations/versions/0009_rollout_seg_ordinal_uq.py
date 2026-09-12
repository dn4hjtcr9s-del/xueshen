"""Rollout 段的 thread/ordinal 唯一约束改为"非 deleted 行唯一"（I-1 修复）。

迁移 ``0008`` 只把 ``turn_id`` 的唯一性放宽成部分唯一索引，``0007`` 建表时留下的
``uq_rollout_segment_thread_ordinal UNIQUE (thread_id, ordinal_start)`` 仍是**覆盖全部
行**的硬约束。0008 同时引入的「跨节点恢复」路径会先 ``mark_deleted`` 旧行、再用同一
``ordinal_start`` 新建段——两步合起来必然撞上这个未被放宽的约束（真实 PG 实测
``duplicate key value violates unique constraint "uq_rollout_segment_thread_ordinal"``），
于是 ``register_open`` 抛错被吞、新段没有 manifest 行、``seal()`` 更新 0 行，
而对象已经上传成孤儿。

本迁移把 ``(thread_id, ordinal_start)`` 与 ``turn_id`` 统一为同款部分唯一索引
（``WHERE status <> 'deleted'``），使"标废旧段 + 复用序号"在约束层面成立。

``downgrade`` 回滚前必须确认重建的**覆盖全部行**的 ``UNIQUE (thread_id, ordinal_start)``
真的建得起来。注意 0008 的守卫在这里是个反例：它只检查"非 deleted 行的重复"，
却重建了覆盖 deleted 行的约束，于是"1 deleted + 1 活跃（同 turn / 同 ordinal）"这种
**它本来要允许**的状态在回滚时会直接抛 UniqueViolation，而不是给出设计好的
``RAISE EXCEPTION``。本迁移的守卫把 deleted 行一并计入（不排除任何行），与将要重建的
约束严格同口径。

> 0008 的守卫缺陷**不在本迁移修正**：历史迁移不回头改（已上线的库不会重跑 0008 的
> downgrade）。是否单独修由后续决定，见交付报告。

ORDER：先建部分唯一索引再删旧约束；Alembic 在同一事务内执行 DDL，窗口不可观测。
"""

from __future__ import annotations

from alembic import op

revision = "0009_rollout_seg_ordinal_uq"
down_revision = "0008_rollout_segment_turn_uq"
branch_labels = None
depends_on = None

_TABLE = "conversation.conversation_rollout_segments"


def upgrade() -> None:
    """把 ``(thread_id, ordinal_start)`` 唯一性放宽为"仅对非 deleted 行唯一"。"""
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_rollout_segment_thread_ordinal_active "
        f"ON {_TABLE} (thread_id, ordinal_start) "
        "WHERE status <> 'deleted'"
    )
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS uq_rollout_segment_thread_ordinal")


def downgrade() -> None:
    """回滚前确认不存在任何"同 thread 同 ordinal_start"的重复行。

    守卫与将要重建的 ``UNIQUE (thread_id, ordinal_start)`` **同口径**：统计时
    **不**排除 ``deleted`` 行。只排除 deleted 行会让"1 deleted + 1 活跃"这种
    跨节点重建留下的正常状态在回滚时撞 UniqueViolation，而不是得到本守卫的
    ``RAISE EXCEPTION`` 提示。
    """
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT thread_id, ordinal_start
                FROM conversation.conversation_rollout_segments
                GROUP BY thread_id, ordinal_start
                HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION
                    '0009_rollout_seg_ordinal_uq 无法回滚：'
                    '存在同一 thread 同一 ordinal_start 的多个段'
                    '（含已 deleted 的 tombstone 行），'
                    '重建 UNIQUE (thread_id, ordinal_start) 会违反唯一性';
            END IF;
        END $$
        """
    )
    op.execute(
        f"ALTER TABLE {_TABLE} "
        "ADD CONSTRAINT uq_rollout_segment_thread_ordinal UNIQUE (thread_id, ordinal_start)"
    )
    op.execute("DROP INDEX IF EXISTS conversation.uq_rollout_segment_thread_ordinal_active")
