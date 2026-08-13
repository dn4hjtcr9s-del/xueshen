"""TurnEventWriter：Turn Event 唯一追加入口（方案 §7.4 / Q8 / 附录 A.6）。

所有写入者（API、Graph Worker、Publisher、MEMORYACK）都必须通过本类：
在调用方事务内锁 Turn 行、原子 +1 last_event_sequence、以新值插入事件，
依赖 (turn_id, sequence) 唯一约束防重。禁止 SELECT MAX(sequence)+1，
也禁止进程内各自维护计数器。
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from backend.conversation.contracts.events import TurnEventWrite, validate_event_payload
from backend.conversation.persistence.events import insert_event
from backend.conversation.persistence.turns import allocate_event_sequence


class IdGeneratorProtocol(Protocol):
    def new_uuid(self) -> UUID: ...


class TurnEventWriter:
    """会话内事件写入器（依赖注入；只操作当前传入的事务）。"""

    def __init__(self, *, id_generator: IdGeneratorProtocol) -> None:
        self._id_generator = id_generator

    async def append(
        self,
        session: AsyncSession,
        *,
        write: TurnEventWrite,
    ) -> dict[str, Any]:
        """在同一数据库事务内追加事件；返回事件行（含 sequence/payload 校验后形状）。"""
        payload = validate_event_payload(write.event_type, write.payload)
        sequence = await allocate_event_sequence(session, write.turn_id)
        event_id = self._id_generator.new_uuid()
        await insert_event(
            session,
            event_id=event_id,
            turn_id=write.turn_id,
            sequence=sequence,
            event_type=write.event_type,
            request_id=write.request_id,
            run_id=write.run_id,
            payload=payload,
        )
        return {
            "event_id": event_id,
            "turn_id": write.turn_id,
            "sequence": sequence,
            "event_type": write.event_type,
            "request_id": write.request_id,
            "run_id": write.run_id,
            "payload": payload,
        }
