"""conversation_turn_events 仓储（方案 §7.4）。

事件插入必须配合 TurnEventWriter 在同一事务内调用：先 allocate_event_sequence
（锁 Turn 行原子 +1），再插入事件，依赖 (turn_id, sequence) 唯一约束防重。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.conversation.contracts.events import validate_event_payload


async def insert_event(
    session: AsyncSession,
    *,
    event_id: UUID,
    turn_id: UUID,
    sequence: int,
    event_type: str,
    request_id: str,
    run_id: str,
    payload: dict[str, object],
    occurred_at: datetime | None = None,
) -> None:
    """插入事件行（调用方必须已分配 sequence 且处于同一事务）。"""
    now = occurred_at or datetime.now(UTC)
    validated = validate_event_payload(event_type, payload)
    await session.execute(
        text(
            "INSERT INTO conversation.conversation_turn_events ("
            "    event_id, turn_id, sequence, event_type, request_id, run_id,"
            "    occurred_at, payload"
            ") VALUES ("
            "    :event_id, :turn_id, :sequence, :event_type, :request_id, :run_id,"
            "    :occurred_at, CAST(:payload AS jsonb)"
            ")"
        ),
        {
            "event_id": event_id,
            "turn_id": turn_id,
            "sequence": sequence,
            "event_type": event_type,
            "request_id": request_id,
            "run_id": run_id,
            "occurred_at": now,
            "payload": json.dumps(validated, ensure_ascii=False),
        },
    )


async def delete_events_for_thread(session: AsyncSession, thread_id: UUID) -> None:
    """删除线程时清理事件（§8.6 步骤 3，经 Turn 关联）。"""
    await session.execute(
        text(
            "DELETE FROM conversation.conversation_turn_events "
            "WHERE turn_id IN ("
            "  SELECT turn_id FROM conversation.conversation_turns "
            "  WHERE thread_id = :thread_id"
            ")"
        ),
        {"thread_id": thread_id},
    )
