"""允许 Conversation Turn Event 持久化安全的执行进度。

前端流程面板依赖 ``turn.progress`` 事件；契约层已支持该类型后，数据库事件类型
检查约束也必须同步放行，否则进度写入会被 PostgreSQL 拒绝并按旁路策略降级。
"""

from __future__ import annotations

from alembic import op

revision = "0006_turn_progress_event"
down_revision = "0005_ks_model_call_attempts"
branch_labels = None
depends_on = None

_EVENT_TYPES = (
    "turn.accepted",
    "turn.started",
    "turn.progress",
    "answer.delta",
    "citation.available",
    "turn.degraded",
    "memory.submission",
    "answer.completed",
    "turn.failed",
    "turn.cancelled",
)


def _replace_event_type_constraint(event_types: tuple[str, ...]) -> None:
    """原子替换事件类型检查约束，保持约束名稳定便于后续迁移。"""
    allowed = ", ".join(f"'{event_type}'" for event_type in event_types)
    op.execute(
        "ALTER TABLE conversation.conversation_turn_events "
        "DROP CONSTRAINT conversation_turn_events_event_type_check, "
        "ADD CONSTRAINT conversation_turn_events_event_type_check "
        f"CHECK (event_type IN ({allowed}))"
    )


def upgrade() -> None:
    """放行不会泄露隐藏推理内容的 ``turn.progress`` 事件。"""
    _replace_event_type_constraint(_EVENT_TYPES)


def downgrade() -> None:
    """存在进度事件时拒绝收窄约束，避免为了回滚静默删除事件。"""
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM conversation.conversation_turn_events
                WHERE event_type = 'turn.progress'
            ) THEN
                RAISE EXCEPTION
                    '0006_turn_progress_event 无法回滚：数据库中已存在 turn.progress 事件';
            END IF;
        END
        $$
        """
    )
    _replace_event_type_constraint(
        tuple(event_type for event_type in _EVENT_TYPES if event_type != "turn.progress")
    )
