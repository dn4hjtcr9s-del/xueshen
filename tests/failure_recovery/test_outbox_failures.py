"""失败恢复测试（§23.4）：Outbox 消费与并发领取故障注入。

注入点（本文件覆盖 5、6、9；8「Worker 心跳停止」由
tests/integration/test_worker_processes.py 的 lease 回收测试覆盖）：
5. Outbox 消费前（delivery 执行失败 → retry_wait，恢复后投递且不重复）
6. Outbox 消费后、标记 published 前（dispatch 与标记同事务回滚，重投不重复）
9. Gateway 快速路径与 Worker 同时领取（FOR UPDATE SKIP LOCKED 只一个赢家）

验收：不丢任务、不重复提交、至少一次执行不造成重复业务效果。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.common import idempotency_payload_hash
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.persistence import operations as ops_repo
from backend.memory.persistence import outbox as outbox_repo
from backend.memory.worker.outbox_consumer import OutboxConsumer

USER = UUID("00000000-0000-4000-8000-0000000000f5")


async def _insert_notification_event(
    session_factory: async_sessionmaker[AsyncSession], *, outbox_id: UUID
) -> None:
    """memory.deleted 事件（含 user_notification target；空图谱候选 → projection 幂等成功）。"""
    async with session_factory() as session:
        async with session.begin():
            inserted = await outbox_repo.insert_event(
                session,
                outbox_id=outbox_id,
                operation_id=None,
                user_id=USER,
                event_type="memory.deleted",
                aggregate_type="memory",
                aggregate_id="mastery:fault",
                aggregate_version=1,
                payload={
                    "schema_version": 1,
                    "memory_id": "mastery:fault",
                    "memory_type": "mastery",
                    "deleted_version": 1,
                    "restore_until": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                    "graph_projection_candidates": [],
                },
            )
            assert inserted


async def _make_claimable(
    session_factory: async_sessionmaker[AsyncSession], outbox_id: UUID
) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE memory_outbox SET next_run_at = now() - interval '1 second' "
                    "WHERE outbox_id = :id"
                ),
                {"id": outbox_id},
            )


async def _notification_count(
    session_factory: async_sessionmaker[AsyncSession], outbox_id: UUID
) -> int:
    async with session_factory() as session:
        row = await session.execute(
            text("SELECT COUNT(*) FROM memory_user_notifications WHERE source_outbox_id = :id"),
            {"id": outbox_id},
        )
        return int(row.scalar_one())


async def test_fail_after_dispatch_before_mark_published_redelivers_once(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注入点 6：dispatch 已执行、标记 delivery 成功前失败。

    dispatch 与标记在同一事务：失败整体回滚，通知不残留；恢复后重投，
    最终只产生一条通知（至少一次执行不造成重复业务效果）。
    """
    outbox_id = uuid4()
    await _insert_notification_event(session_factory, outbox_id=outbox_id)
    consumer = OutboxConsumer(session_factory=session_factory)

    async with session_factory() as session:
        deliveries = await outbox_repo.list_deliveries(session, outbox_id=outbox_id)
    notification_delivery_id = next(
        d["delivery_id"] for d in deliveries if d["target"] == "user_notification"
    )

    original_mark = outbox_repo.mark_delivery
    fired = {"done": False}

    async def flaky_mark(session: Any, **kwargs: Any) -> None:
        if kwargs.get("delivery_id") == notification_delivery_id and not fired["done"]:
            fired["done"] = True
            raise RuntimeError("injected: mark_delivery failure")
        await original_mark(session, **kwargs)

    monkeypatch.setattr(outbox_repo, "mark_delivery", flaky_mark)
    await consumer.tick()

    # 注入失败的通知 delivery 已回滚：不得残留通知，也不得 published
    assert await _notification_count(session_factory, outbox_id) == 0
    async with session_factory() as session:
        status = await outbox_repo.get_status(session, outbox_id=outbox_id)
    assert status != "published"

    monkeypatch.undo()
    await _make_claimable(session_factory, outbox_id)
    await consumer.tick()
    async with session_factory() as session:
        status = await outbox_repo.get_status(session, outbox_id=outbox_id)
    assert status == "published"
    assert await _notification_count(session_factory, outbox_id) == 1


async def test_concurrent_claim_only_one_winner(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """注入点 9：Gateway 快速路径与 Worker 同时领取同一 operation。

    FOR UPDATE SKIP LOCKED 保证只有一个执行者领取成功，任务不丢也不重复执行。
    """
    operation = MemoryOperation(
        operation_id=uuid4(),
        idempotency_key=f"idem-{uuid4().hex[:12]}",
        user_id=USER,
        actor_type="user",
        input_kind="command",
        operation_type="correct_memory",
        priority=100,
        occurred_at=datetime.now(UTC),
        payload={"kind": "forget_memory", "memory_id": "learner", "expected_version": 1},
        trace_id=uuid4().hex + uuid4().hex,
        graph_thread_id=f"memory-op:{uuid4()}",
    )
    async with session_factory() as session:
        async with session.begin():
            await ops_repo.insert_operation(
                session,
                operation,
                idempotency_payload_hash=idempotency_payload_hash(
                    operation.payload.model_dump(mode="json")
                ),
            )

    async def claim(worker_id: str) -> list[dict[str, Any]]:
        async with session_factory() as session:
            async with session.begin():
                return await ops_repo.claim_operation(
                    session, worker_id=worker_id, lease_seconds=30
                )

    gateway_claim, worker_claim = await asyncio.gather(
        claim("gateway-fast-path"), claim("worker-1")
    )
    winners = [c for c in (gateway_claim, worker_claim) if c]
    # 只有一个赢家领取到该任务（§14.2 同一句 claim SQL）
    assert len(winners) == 1
    assert len(winners[0]) == 1
    assert winners[0][0]["operation_id"] == operation.operation_id
    # 领取后状态为 running，任务未丢失
    async with session_factory() as session:
        row = await ops_repo.get_operation(session, operation.operation_id)
    assert row is not None and row["status"] == "running"
