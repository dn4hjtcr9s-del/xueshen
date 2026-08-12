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

from backend.memory.contracts.commands import (
    CommitMutationPlan,
    LearnerPatch,
    MaintenanceCommand,
)
from backend.memory.contracts.common import SYSTEM_MAINTENANCE_USER_ID
from backend.memory.contracts.errors import (
    LeaseFencedError,
    OperationCancelNotAllowedError,
)
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.graph.state import MemoryRuntimeContext
from backend.memory.persistence import maintenance as maintenance_repo
from backend.memory.persistence import notifications as notifications_repo
from backend.memory.persistence import operations as ops_repo
from backend.memory.persistence import outbox as outbox_repo
from backend.memory.services.memory_service import MemoryService
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
        # 共享测试库的 langgraph 表可能有其他运行遗留的孤儿线程（评审 P0-1 引入
        # 孤儿清扫），断言目标线程被删除而非精确相等
        assert thread_id_for_operation(old.operation_id) in saver.deleted
        async with session_factory() as session:
            run_row = await maintenance_repo.get_run_by_key(
                session, idempotency_key="cleanup-checkpoints:integration-test"
            )
        assert run_row is not None
        assert run_row["status"] == "succeeded"
        assert run_row["result"]["kind"] == "cleanup_checkpoints"
        assert run_row["result"]["threads_deleted"] == 1


class TestLeaseFencing:
    """评审 #7/#8：lease_generation fencing token——Lease 易主后，
    旧持有者的心跳/完成/重排/delivery 写回必须全部失败且不覆盖新状态。"""

    async def _reclaim_operation(
        self, session_factory: async_sessionmaker[AsyncSession], operation_id: UUID
    ) -> dict[str, Any]:
        """过期 → Scheduler 回收 → worker-2 重新领取，返回新行。"""
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE memory_operations "
                        "SET lease_expires_at = now() - interval '1 second' "
                        "WHERE operation_id = :id"
                    ),
                    {"id": operation_id},
                )
        async with session_factory() as session:
            async with session.begin():
                assert await ops_repo.recover_expired_leases(session) == 1
        async with session_factory() as session:
            async with session.begin():
                reclaimed = await ops_repo.claim_operation(
                    session, worker_id="worker-2", lease_seconds=120
                )
        return reclaimed[0]

    async def test_stale_worker_writebacks_rejected_after_reclaim(
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
        gen1 = int(claimed[0]["lease_generation"])

        reclaimed = await self._reclaim_operation(session_factory, operation.operation_id)
        gen2 = int(reclaimed["lease_generation"])
        assert gen2 > gen1, "重新领取必须递增 fencing generation"

        # 旧持有者：心跳 / 完成 / 重排全部被 fencing 拒绝
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await ops_repo.heartbeat(
                        session,
                        operation_id=operation.operation_id,
                        worker_id="worker-1",
                        lease_seconds=120,
                        generation=gen1,
                    )
                    is False
                )
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await ops_repo.complete_operation(
                        session,
                        operation_id=operation.operation_id,
                        status="succeeded",
                        result={"stale": True},
                        public_error=None,
                        expected_worker="worker-1",
                        expected_generation=gen1,
                    )
                    is False
                )
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await ops_repo.reschedule_operation(
                        session,
                        operation_id=operation.operation_id,
                        next_run_at=datetime.now(UTC),
                        status="retry_wait",
                        expected_worker="worker-1",
                        expected_generation=gen1,
                    )
                    is False
                )

        # 新持有者状态未被覆盖
        async with session_factory() as session:
            row = await ops_repo.get_operation(session, operation.operation_id)
        assert row is not None
        assert row["status"] == "running"
        assert row["locked_by"] == "worker-2"

        # 新持有者写回成功
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await ops_repo.complete_operation(
                        session,
                        operation_id=operation.operation_id,
                        status="succeeded",
                        result={"fresh": True},
                        public_error=None,
                        expected_worker="worker-2",
                        expected_generation=gen2,
                    )
                    is True
                )
        async with session_factory() as session:
            row = await ops_repo.get_operation(session, operation.operation_id)
        assert row is not None and row["status"] == "succeeded"

    async def test_stale_consumer_writebacks_rejected_after_reclaim(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        outbox_id = uuid4()
        await _insert_memory_deleted_event(
            session_factory,
            outbox_id=outbox_id,
            memory_id="mastery:fencing",
            deleted_version=1,
            candidates=[],
        )
        async with session_factory() as session:
            async with session.begin():
                claimed = await outbox_repo.claim_batch(
                    session, worker_id="consumer-1", lease_seconds=60, batch_size=100
                )
        gen1 = int(claimed[0]["lease_generation"])

        # Lease 过期回收 → consumer-2 重新领取
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
        gen2 = int(reclaimed[0]["lease_generation"])
        assert gen2 > gen1

        async with session_factory() as session:
            deliveries = await outbox_repo.list_deliveries(session, outbox_id=outbox_id)
        delivery = deliveries[0]

        # 旧 Consumer：delivery 标记 / finalize / 重排全部被 fencing 拒绝
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await outbox_repo.mark_delivery(
                        session,
                        delivery_id=delivery["delivery_id"],
                        status="succeeded",
                        expected_worker="consumer-1",
                        expected_generation=gen1,
                    )
                    is False
                )
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await outbox_repo.finalize_outbox(
                        session,
                        outbox_id=outbox_id,
                        expected_worker="consumer-1",
                        expected_generation=gen1,
                    )
                    is False
                )
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await outbox_repo.reschedule_outbox(
                        session,
                        outbox_id=outbox_id,
                        next_run_at=datetime.now(UTC),
                        expected_worker="consumer-1",
                        expected_generation=gen1,
                    )
                    is False
                )

        # 主行与 delivery 未被覆盖
        row = await _outbox_row(session_factory, outbox_id)
        assert row["status"] == "publishing"
        assert row["locked_by"] == "consumer-2"
        async with session_factory() as session:
            deliveries = await outbox_repo.list_deliveries(session, outbox_id=outbox_id)
        assert all(d["status"] == "pending" for d in deliveries)

        # 新持有者写回成功
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await outbox_repo.mark_delivery(
                        session,
                        delivery_id=delivery["delivery_id"],
                        status="succeeded",
                        expected_worker="consumer-2",
                        expected_generation=gen2,
                    )
                    is True
                )


class TestCommitMarkerFencing:
    """评审二轮 #3：commit marker 使用 lease fencing token——Lease 易主后，
    旧持有者不得设置/清除新持有者的 marker，也不得进入业务提交路径。"""

    async def _make_and_claim(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        worker_id: str,
        lease_seconds: int = 120,
    ) -> tuple[UUID, dict[str, Any]]:
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
                    session, worker_id=worker_id, lease_seconds=lease_seconds
                )
        assert [r["operation_id"] for r in claimed] == [operation.operation_id]
        return operation.operation_id, claimed[0]

    async def _reclaim(
        self, session_factory: async_sessionmaker[AsyncSession], operation_id: UUID
    ) -> dict[str, Any]:
        """旧 Lease 过期 → Scheduler 回收 → worker-b 重新领取。"""
        async with session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE memory_operations "
                        "SET lease_expires_at = now() - interval '1 second' "
                        "WHERE operation_id = :id"
                    ),
                    {"id": operation_id},
                )
        async with session_factory() as session:
            async with session.begin():
                assert await ops_repo.recover_expired_leases(session) == 1
        async with session_factory() as session:
            async with session.begin():
                reclaimed = await ops_repo.claim_operation(
                    session, worker_id="worker-b", lease_seconds=120
                )
        return reclaimed[0]

    async def test_stale_worker_cannot_clear_or_mark_after_reclaim(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """B mark 后 A clear → 失败且 marker 保留；B 持有时 A mark → 失败。"""
        operation_id, row_a = await self._make_and_claim(session_factory, worker_id="worker-a")
        gen_a = int(row_a["lease_generation"])

        row_b = await self._reclaim(session_factory, operation_id)
        gen_b = int(row_b["lease_generation"])
        assert gen_b > gen_a

        # 新持有者 B 设置 marker 成功
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await ops_repo.mark_commit_started(
                        session,
                        operation_id=operation_id,
                        expected_worker="worker-b",
                        expected_generation=gen_b,
                    )
                    is True
                )
        # 旧持有者 A 的迟到 clear 被 fencing 拒绝，B 的 marker 保留
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await ops_repo.clear_commit_started(
                        session,
                        operation_id=operation_id,
                        expected_worker="worker-a",
                        expected_generation=gen_a,
                    )
                    is False
                )
        async with session_factory() as session:
            row = await ops_repo.get_operation(session, operation_id)
        assert row is not None
        assert row["commit_started_at"] is not None
        assert row["locked_by"] == "worker-b"

        # B 持有 Lease 时 A 设置 marker 同样被拒
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await ops_repo.mark_commit_started(
                        session,
                        operation_id=operation_id,
                        expected_worker="worker-a",
                        expected_generation=gen_a,
                    )
                    is False
                )

        # 新持有者 B 的 clear 成功
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await ops_repo.clear_commit_started(
                        session,
                        operation_id=operation_id,
                        expected_worker="worker-b",
                        expected_generation=gen_b,
                    )
                    is True
                )
        async with session_factory() as session:
            row = await ops_repo.get_operation(session, operation_id)
        assert row is not None and row["commit_started_at"] is None

    async def test_fenced_commit_plans_raises_before_business_effects(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        memory_service: MemoryService,
    ) -> None:
        """marker CAS 失败后旧 Worker 调 commit_plans → LeaseFencedError，
        数据库业务副作用（commits/documents）不发生。"""
        operation_id, row_a = await self._make_and_claim(session_factory, worker_id="worker-a")
        gen_a = int(row_a["lease_generation"])
        await self._reclaim(session_factory, operation_id)

        plans = [
            CommitMutationPlan(
                mutation_id=uuid4(),
                memory_id="learner",
                target_memory_type="learner",
                action="create",
                learner_patch=LearnerPatch(goals_to_add=["不应落库的目标"]),
            )
        ]
        with pytest.raises(LeaseFencedError):
            await memory_service.commit_plans(
                operation_id=operation_id,
                user_id=USER,
                actor_type="user",
                plans=plans,
                expected_worker="worker-a",
                expected_generation=gen_a,
            )
        async with session_factory() as session:
            for table in ("memory_commits", "memory_documents"):
                result = await session.execute(text(f"SELECT count(*) FROM {table}"))
                assert int(result.scalar_one()) == 0, f"{table} 不得有业务副作用"

    async def test_recovered_lease_clears_stale_marker_then_cancel_allowed(
        self, session_factory: async_sessionmaker[AsyncSession]
    ) -> None:
        """§11.6 + 评审二轮 #3：A 崩溃残留 marker 时 cancel → 409；Lease 回收后
        新持有者 B 在执行开始清掉残留 marker，此后 cancel 恢复协作受理。"""
        operation_id, row_a = await self._make_and_claim(session_factory, worker_id="worker-a")
        gen_a = int(row_a["lease_generation"])
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await ops_repo.mark_commit_started(
                        session,
                        operation_id=operation_id,
                        expected_worker="worker-a",
                        expected_generation=gen_a,
                    )
                    is True
                )
        # marker 在位：cancel 仲裁拒绝
        async with session_factory() as session:
            async with session.begin():
                with pytest.raises(OperationCancelNotAllowedError):
                    await ops_repo.request_cancel(session, operation_id=operation_id)

        # A 崩溃 → Lease 过期回收 → B 领取；B 执行开始的 fencing clear 清掉残留
        row_b = await self._reclaim(session_factory, operation_id)
        gen_b = int(row_b["lease_generation"])
        async with session_factory() as session:
            async with session.begin():
                assert (
                    await ops_repo.clear_commit_started(
                        session,
                        operation_id=operation_id,
                        expected_worker="worker-b",
                        expected_generation=gen_b,
                    )
                    is True
                )
        # marker 已清除：协作取消受理
        async with session_factory() as session:
            async with session.begin():
                row = await ops_repo.request_cancel(session, operation_id=operation_id)
        assert row is not None
        assert row["cancel_requested_at"] is not None
