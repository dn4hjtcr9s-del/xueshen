"""Graph 集成测试辅助：operation 工厂与持久化。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import MemoryPayload
from backend.memory.contracts.common import (
    ActorType,
    InputKind,
    OperationType,
    idempotency_payload_hash,
)
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.persistence.operations import insert_operation


def make_operation(
    *,
    user_id: UUID,
    actor_type: ActorType,
    input_kind: InputKind,
    operation_type: OperationType,
    priority: int,
    payload: MemoryPayload,
    idempotency_key: str | None = None,
) -> MemoryOperation:
    return MemoryOperation(
        operation_id=uuid4(),
        idempotency_key=idempotency_key or f"idem-{uuid4().hex[:12]}",
        user_id=user_id,
        actor_type=actor_type,
        input_kind=input_kind,
        operation_type=operation_type,
        priority=priority,
        occurred_at=datetime.now(UTC),
        payload=payload,
        trace_id=uuid4().hex + uuid4().hex,
        graph_thread_id=f"graph-{uuid4().hex[:12]}",
    )


async def persist_operation(
    session_factory: async_sessionmaker[AsyncSession], operation: MemoryOperation
) -> None:
    """写入 operation 行（memory_commits 外键与幂等检查需要）。"""
    async with session_factory() as session:
        async with session.begin():
            await insert_operation(
                session,
                operation,
                idempotency_payload_hash=idempotency_payload_hash(
                    operation.payload.model_dump(mode="json")
                ),
            )
