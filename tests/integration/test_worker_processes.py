"""Worker/Scheduler/Outbox Consumer 集成测试（§23.3 / §23.4）：真实 PostgreSQL。

覆盖：
- Outbox delivery 多目标部分失败可恢复（§23.3）；
- 过期 Lease 回收后可重新领取（§11.5 / §14.3）；
- checkpoint 清理查询不选中 running 中的 operation（§11.4）；
- cleanup_checkpoints 维护分支经 Graph 执行并回写 maintenance run（§10.7 / §14.3）。
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import MaintenanceCommand
from backend.memory.contracts.common import SYSTEM_MAINTENANCE_USER_ID
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.graph.state import MemoryRuntimeContext
from backend.memory.persistence import maintenance as maintenance_repo
from backend.memory.persistence import notifications as notifications_repo
from backend.memory.persistence import operations as ops_repo
from backend.memory.persistence import outbox as outbox_repo
from backend.memory.worker.checkpoint import (
    CheckpointCleanupAdapter,
    list_expired_checkpoint_threads,
    thread_id_for_operation,
)
from backend.memory.worker.outbox_consumer import OutboxConsumer
from tests.integration.graph_helpers import make_operation, persist_operation

USER = UUID("00000000-0000-4000-8000-000000000088")


async def _insert_memory_deleted_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    outbox_id: UUID,
    memory_id: str,
    deleted_version: int,
    candidates: list[str],
) -> None:
    async with session_factory() as session:
        async with session.begin():
            inserted = await outbox_repo.insert_event(
                session,
                outbox_id=outbox_id,
                operation_id=None,
                user_id=USER,
                event_type="memory.deleted",
                aggregate_type="memory",
                aggregate_id=memory_id,
                aggregate_version=deleted_version,
                payload={
                    "schema_version": 1,
                    "memory_id": memory_id,
                    "memory_type": "mastery",
                    "deleted_version": deleted_version,
                    "restore_until": (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                    "graph_projection_candidates": candidates,
                },
            )
            assert inserted


async def _outbox_row(
    session_factory: async_sessionmaker[AsyncSession], outbox_id: UUID
) -> dict[str, Any]:
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT * FROM memory_outbox WHERE outbox_id = :id"), {"id": outbox_id}
        )
        row = result.mappings().one()
        return dict(row)


async def _deliveries(
    session_factory: async_sessionmaker[AsyncSession], outbox_id: UUID
) -> dict[str, dict[str, Any]]:
    async with session_factory() as session:
        rows = await outbox_repo.list_deliveries(session, outbox_id=outbox_id)
        return {str(r["target"]): r for r in rows}


async def _make_claimable(
    session_factory: async_sessionmaker[AsyncSession], outbox_id: UUID
) -> None:
    """退避后强制到期，模拟重试时间点到达。"""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE memory_outbox SET next_run_at = now() - interval '1 second' "
                    "WHERE outbox_id = :id"
                ),
                {"id": outbox_id},
            )


class TestOutboxDeliveryRecovery:
    """§23.3：Outbox delivery 多目标部分失败可恢复。"""

    async def test_partial_failure_recovers_on_retry(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        outbox_id = uuid4()
        await _insert_memory_deleted_event(
            session_factory,
            outbox_id=outbox_id,
            memory_id="mastery:partial",
            deleted_version=2,
            candidates=[],  # 空候选：summary_projection 幂等成功，聚焦通知失败注入
        )
        consumer = OutboxConsumer(session_factory=session_factory)

        # 第一次 tick：user_notification target 注入失败
        async def failing_notification(session: Any, **kwargs: Any) -> None:
            raise RuntimeError("注入的通知写入失败")

        monkeypatch.setattr(notifications_repo, "insert_notification", failing_notification)
        assert await consumer.tick() == 1

        row = await _outbox_row(session_factory, outbox_id)
        assert row["status"] == "retry_wait"  # 未全部成功不得 published（§13.12）
        deliveries = await _deliveries(session_factory, outbox_id)
        assert deliveries["summary_projection"]["status"] == "succeeded"
        assert deliveries["internal_event_log"]["status"] == "succeeded"
        assert deliveries["user_notification"]["status"] == "retry_wait"

        # 第二次 tick：故障恢复，退避到期后重新领取，全成功后 published
        monkeypatch.undo()
        await _make_claimable(session_factory, outbox_id)
        assert await consumer.tick() == 1

        row = await _outbox_row(session_factory, outbox_id)
        assert row["status"] == "published"
        assert row["published_at"] is not None
        deliveries = await _deliveries(session_factory, outbox_id)
        assert all(d["status"] == "succeeded" for d in deliveries.values())

        # 至少一次投递不产生重复业务效果：通知与内部事件各一行
        async with session_factory() as session:
            count = await session.execute(
                text("SELECT COUNT(*) FROM memory_user_notifications WHERE source_outbox_id = :id"),
                {"id": outbox_id},
            )
            assert count.scalar_one() == 1
            count = await session.execute(
                text("SELECT COUNT(*) FROM memory_internal_event_log WHERE outbox_id = :id"),
                {"id": outbox_id},
            )
            assert count.scalar_one() == 1

    async def test_summary_projection_creates_operations_idempotently(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """§14.4：memory.deleted 为每个候选节点创建 recompute projection operation。"""
        outbox_id = uuid4()
        await _insert_memory_deleted_event(
            session_factory,
            outbox_id=outbox_id,
            memory_id="mastery:proj",
            deleted_version=3,
            candidates=["n001", "n002"],
        )
        consumer = OutboxConsumer(session_factory=session_factory)
        assert await consumer.tick() == 1
        assert (await _outbox_row(session_factory, outbox_id))["status"] == "published"

        async with session_factory() as session:
            result = await session.execute(
                text(
                    "SELECT idempotency_key, actor_type, input_kind, operation_type, payload "
                    "FROM memory_operations WHERE actor_type = 'summary_projection' "
                    "ORDER BY idempotency_key"
                )
            )
            ops = [dict(r) for r in result.mappings().all()]
        assert [op["idempotency_key"] for op in ops] == [
            "summary-projection:mastery:proj:3:n001",
            "summary-projection:mastery:proj:3:n002",
        ]
        for op in ops:
            assert op["input_kind"] == "projection"
            assert op["operation_type"] == "project_summary_to_graph"
            assert op["payload"]["projection_action"] == "recompute_without_deleted_version"
            assert op["payload"]["source_version"] == 3

        # 重放同一主行（至少一次）：delivery 已 succeeded，不重复创建 operation
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE memory_outbox SET status = 'pending', "
                        "next_run_at = now() - interval '1 second' WHERE outbox_id = :id"
                    ),
                    {"id": outbox_id},
                )
        assert await consumer.tick() == 1
        async with session_factory() as session:
            count = await session.execute(
                text(
                    "SELECT COUNT(*) FROM memory_operations WHERE actor_type = 'summary_projection'"
                )
            )
            assert count.scalar_one() == 2
        assert (await _outbox_row(session_factory, outbox_id))["status"] == "published"


class TestLeaseRecovery:
    """§11.5 / §14.3：过期 Lease 回收后可重新领取。"""

    async def test_operation_lease_recovered_and_reclaimed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        operation = make_operation(
            user_id=USER,
            actor_type="system",
            input_kind="maintenance",
            operation_type="purge_tombstones",
            priority=0,
            payload=MaintenanceCommand(kind="purge_tombstones"),
        )
        await persist_operation(session_factory, operation)

        async with session_factory() as session:
            async with session.begin():
                claimed = await ops_repo.claim_operation(
                    session, worker_id="worker-1", lease_seconds=120
                )
        assert [r["operation_id"] for r in claimed] == [operation.operation_id]

        # running 且 Lease 未过期：其他 worker 领不到
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await ops_repo.claim_operation(session, worker_id="worker-2", lease_seconds=120)
                ) == []

        # Lease 过期 → Scheduler 回收 → 可重新领取
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE memory_operations "
                        "SET lease_expires_at = now() - interval '1 second' "
                        "WHERE operation_id = :id"
                    ),
                    {"id": operation.operation_id},
                )
        async with session_factory() as session:
            async with session.begin():
                assert await ops_repo.recover_expired_leases(session) == 1
        async with session_factory() as session:
            async with session.begin():
                reclaimed = await ops_repo.claim_operation(
                    session, worker_id="worker-2", lease_seconds=120
                )
        assert [r["operation_id"] for r in reclaimed] == [operation.operation_id]

    async def test_outbox_lease_recovered_and_reclaimed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        outbox_id = uuid4()
        await _insert_memory_deleted_event(
            session_factory,
            outbox_id=outbox_id,
            memory_id="mastery:lease",
            deleted_version=1,
            candidates=[],
        )
        async with session_factory() as session:
            async with session.begin():
                claimed = await outbox_repo.claim_batch(
                    session, worker_id="consumer-1", lease_seconds=60, batch_size=100
                )
        assert [r["outbox_id"] for r in claimed] == [outbox_id]

        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE memory_outbox "
                        "SET lease_expires_at = now() - interval '1 second' "
                        "WHERE outbox_id = :id"
                    ),
                    {"id": outbox_id},
                )
        async with session_factory() as session:
            async with session.begin():
                assert await outbox_repo.recover_expired_leases(session) == 1
        async with session_factory() as session:
            async with session.begin():
                reclaimed = await outbox_repo.claim_batch(
                    session, worker_id="consumer-2", lease_seconds=60, batch_size=100
                )
        assert [r["outbox_id"] for r in reclaimed] == [outbox_id]


class TestCheckpointCleanupQuery:
    """§11.4：清理查询不选中 running / Lease 未过期的 operation。"""

    async def _operation_with_status(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        status: str,
        completed_days_ago: float | None,
        lease_future: bool = False,
    ) -> UUID:
        operation = make_operation(
            user_id=USER,
            actor_type="system",
            input_kind="maintenance",
            operation_type="purge_tombstones",
            priority=0,
            payload=MaintenanceCommand(kind="purge_tombstones"),
        )
        await persist_operation(session_factory, operation)
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE memory_operations SET status = :status, "
                        "completed_at = :completed_at, "
                        "lease_expires_at = :lease_expires_at "
                        "WHERE operation_id = :id"
                    ),
                    {
                        "id": operation.operation_id,
                        "status": status,
                        "completed_at": (
                            datetime.now(UTC) - timedelta(days=completed_days_ago)
                            if completed_days_ago is not None
                            else None
                        ),
                        "lease_expires_at": (
                            datetime.now(UTC) + timedelta(hours=1) if lease_future else None
                        ),
                    },
                )
        return operation.operation_id

    async def test_expired_terminal_threads_selected_running_excluded(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        running = await self._operation_with_status(
            session_factory, status="running", completed_days_ago=None, lease_future=True
        )
        succeeded_old = await self._operation_with_status(
            session_factory, status="succeeded", completed_days_ago=8
        )
        succeeded_recent = await self._operation_with_status(
            session_factory, status="succeeded", completed_days_ago=1
        )
        review_old = await self._operation_with_status(
            session_factory, status="needs_review", completed_days_ago=31
        )
        review_recent = await self._operation_with_status(
            session_factory, status="needs_review", completed_days_ago=10
        )
        dead_old = await self._operation_with_status(
            session_factory, status="dead_letter", completed_days_ago=31
        )
        cancelled_old = await self._operation_with_status(
            session_factory, status="cancelled", completed_days_ago=8
        )

        async with session_factory() as session:
            rows = await list_expired_checkpoint_threads(
                session, now=datetime.now(UTC), batch_size=100
            )
        thread_ids = {row["thread_id"] for row in rows}
        # terminal 7 天、needs_review/dead_letter 30 天保留期后入选（§11.4）
        assert thread_id_for_operation(succeeded_old) in thread_ids
        assert thread_id_for_operation(review_old) in thread_ids
        assert thread_id_for_operation(dead_old) in thread_ids
        assert thread_id_for_operation(cancelled_old) in thread_ids
        # running（Lease 未过期）与保留期内的一律不选中
        assert thread_id_for_operation(running) not in thread_ids
        assert thread_id_for_operation(succeeded_recent) not in thread_ids
        assert thread_id_for_operation(review_recent) not in thread_ids

        # cursor 推进：第二批不再重复第一批
        async with session_factory() as session:
            second = await list_expired_checkpoint_threads(
                session, now=datetime.now(UTC), batch_size=2, cursor=rows[1]["cursor"]
            )
        assert {row["cursor"] for row in second}.isdisjoint({row["cursor"] for row in rows[:2]})


class _FakeSaver:
    """记录 adelete_thread 调用的假 checkpointer。"""

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def setup(self) -> None:
        return None

    async def adelete_thread(self, thread_id: str) -> None:
        self.deleted.append(thread_id)


class TestMaintenanceGraphBranch:
    """§10.7 / §14.3：cleanup_checkpoints 经 Graph 有界 batch 执行并回写 run。"""

    async def test_cleanup_checkpoints_deletes_and_writes_back_run(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        runtime_context: MemoryRuntimeContext,
    ) -> None:
        # 一个 8 天前完成的 terminal operation：checkpoint 已过 7 天保留期
        old = make_operation(
            user_id=USER,
            actor_type="system",
            input_kind="maintenance",
            operation_type="purge_tombstones",
            priority=0,
            payload=MaintenanceCommand(kind="purge_tombstones"),
        )
        await persist_operation(session_factory, old)
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE memory_operations SET status = 'succeeded', "
                        "completed_at = now() - interval '8 days' WHERE operation_id = :id"
                    ),
                    {"id": old.operation_id},
                )

        # Scheduler 侧：先创建 maintenance run，再创建并关联 Graph operation（§14.3）
        maint_op = make_operation(
            user_id=UUID(SYSTEM_MAINTENANCE_USER_ID),
            actor_type="system",
            input_kind="maintenance",
            operation_type="cleanup_checkpoints",
            priority=0,
            payload=MaintenanceCommand(kind="cleanup_checkpoints", batch_size=10),
        )
        await persist_operation(session_factory, maint_op)
        async with session_factory() as session:
            async with session.begin():
                run, created = await maintenance_repo.create_or_reuse_run(
                    session,
                    run_id=uuid4(),
                    maintenance_type="cleanup_checkpoints",
                    idempotency_key="cleanup-checkpoints:integration-test",
                )
                assert created
                await maintenance_repo.attach_operation(
                    session, run_id=run["run_id"], operation_id=maint_op.operation_id
                )

        saver = _FakeSaver()
        context = dataclasses.replace(
            runtime_context, checkpoint_cleanup=CheckpointCleanupAdapter(saver=saver)
        )
        runner = LocalLangGraphRunner(context=context)
        result = await runner.run(maint_op)

        assert result.status == "succeeded"
        assert saver.deleted == [thread_id_for_operation(old.operation_id)]
        async with session_factory() as session:
            run_row = await maintenance_repo.get_run_by_key(
                session, idempotency_key="cleanup-checkpoints:integration-test"
            )
        assert run_row is not None
        assert run_row["status"] == "succeeded"
        assert run_row["result"]["kind"] == "cleanup_checkpoints"
        assert run_row["result"]["threads_deleted"] == 1
