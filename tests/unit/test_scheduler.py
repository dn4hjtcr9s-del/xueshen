"""Scheduler 单元测试（§14.3 / §23.1）。

仓储函数 monkeypatch 为内存实现；时钟注入。覆盖：
maintenance run 幂等创建/复用、只有入 Graph 的 batch 才建 operation、
lease 回收调用、日任务时间计算（Asia/Shanghai）。
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest

from backend.memory.contracts.commands import MaintenanceCommand
from backend.memory.persistence import documents as docs_repo
from backend.memory.persistence import maintenance as maintenance_repo
from backend.memory.persistence import notifications as notifications_repo
from backend.memory.persistence import operations as ops_repo
from backend.memory.persistence import outbox as outbox_repo
from backend.memory.worker import scheduler as scheduler_mod
from backend.memory.worker.scheduler import Scheduler, SchedulerConfig
from tests.unit.worker_fakes import make_session_factory

SYSTEM_USER = UUID("00000000-0000-0000-0000-000000000000")
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)  # 20:00 Asia/Shanghai


class _SchedulerRecorder:
    def __init__(self) -> None:
        self.runs: dict[str, dict[str, Any]] = {}
        self.inserted_operations: list[Any] = []
        self.attached: list[tuple[Any, Any]] = []
        self.completed_runs: list[dict[str, Any]] = []
        self.operations_by_id: dict[Any, dict[str, Any]] = {}
        self.operations_by_key: dict[tuple[Any, str, str], dict[str, Any]] = {}
        self.op_leases_recovered = 0
        self.outbox_leases_recovered = 0
        self.dirty_indexes: list[dict[str, Any]] = []
        self.expired_tombstones: list[dict[str, Any]] = []
        self.expired_checkpoints: list[dict[str, Any]] = []
        self.active_documents: list[dict[str, Any]] = []
        self.notification_purges: list[dict[str, Any]] = []
        self.notification_purge_result = 0
        self.backup_ok = True
        self.dead_letters = {"operations": 0, "outbox": 0}

    def add_run(self, run: dict[str, Any]) -> None:
        self.runs[run["idempotency_key"]] = run


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _SchedulerRecorder:
    rec = _SchedulerRecorder()

    async def fake_create_or_reuse(
        session: Any, *, run_id: Any, maintenance_type: str, idempotency_key: str
    ) -> tuple[dict[str, Any], bool]:
        existing = rec.runs.get(idempotency_key)
        if existing is not None:
            return existing, False
        run = {
            "run_id": run_id,
            "maintenance_type": maintenance_type,
            "idempotency_key": idempotency_key,
            "status": "queued",
            "cursor": None,
            "operation_id": None,
        }
        rec.add_run(run)
        return run, True

    async def fake_attach(session: Any, *, run_id: Any, operation_id: Any) -> None:
        rec.attached.append((run_id, operation_id))
        for run in rec.runs.values():
            if run["run_id"] == run_id:
                run["operation_id"] = operation_id

    async def fake_complete_run(session: Any, **kwargs: Any) -> None:
        rec.completed_runs.append(kwargs)
        for run in rec.runs.values():
            if run["run_id"] == kwargs["run_id"]:
                run["status"] = kwargs["status"]

    async def fake_insert_operation(session: Any, operation: Any, **kwargs: Any) -> bool:
        rec.inserted_operations.append(operation)
        row = {"operation_id": operation.operation_id, "status": "queued"}
        rec.operations_by_id[operation.operation_id] = row
        rec.operations_by_key[(operation.user_id, "system", operation.idempotency_key)] = row
        return True

    async def fake_get_operation(session: Any, operation_id: Any) -> dict[str, Any] | None:
        return rec.operations_by_id.get(operation_id)

    async def fake_get_by_idempotency(
        session: Any, *, user_id: Any, actor_type: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        return rec.operations_by_key.get((user_id, actor_type, idempotency_key))

    async def fake_recover_ops(session: Any) -> int:
        return rec.op_leases_recovered

    async def fake_recover_outbox(session: Any) -> int:
        return rec.outbox_leases_recovered

    async def fake_dirty_indexes(session: Any, *, batch_size: int) -> list[dict[str, Any]]:
        return rec.dirty_indexes

    async def fake_tombstones(session: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return rec.expired_tombstones

    async def fake_active_documents(session: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return rec.active_documents

    async def fake_checkpoints(session: Any, **kwargs: Any) -> list[dict[str, Any]]:
        return rec.expired_checkpoints

    async def fake_purge_notifications(session: Any, **kwargs: Any) -> int:
        rec.notification_purges.append(kwargs)
        return rec.notification_purge_result

    async def fake_backup_ok(session: Any, *, since: Any) -> bool:
        return rec.backup_ok

    async def fake_dead_letters(session: Any) -> dict[str, int]:
        return rec.dead_letters

    monkeypatch.setattr(maintenance_repo, "create_or_reuse_run", fake_create_or_reuse)
    monkeypatch.setattr(maintenance_repo, "attach_operation", fake_attach)
    monkeypatch.setattr(maintenance_repo, "complete_run", fake_complete_run)
    monkeypatch.setattr(maintenance_repo, "count_dead_letters", fake_dead_letters)
    monkeypatch.setattr(maintenance_repo, "has_successful_backup_since", fake_backup_ok)
    monkeypatch.setattr(ops_repo, "insert_operation", fake_insert_operation)
    monkeypatch.setattr(ops_repo, "get_operation", fake_get_operation)
    monkeypatch.setattr(ops_repo, "get_by_idempotency", fake_get_by_idempotency)
    monkeypatch.setattr(ops_repo, "recover_expired_leases", fake_recover_ops)
    monkeypatch.setattr(outbox_repo, "recover_expired_leases", fake_recover_outbox)
    monkeypatch.setattr(docs_repo, "list_dirty_indexes", fake_dirty_indexes)
    monkeypatch.setattr(docs_repo, "list_expired_tombstones", fake_tombstones)
    monkeypatch.setattr(docs_repo, "list_active_documents_page", fake_active_documents)
    monkeypatch.setattr(notifications_repo, "purge_older_than", fake_purge_notifications)
    monkeypatch.setattr(scheduler_mod, "list_expired_checkpoint_threads", fake_checkpoints)
    return rec


def _scheduler() -> Scheduler:
    return Scheduler(
        session_factory=make_session_factory(),
        config=SchedulerConfig(),
        clock=lambda: NOW,
    )


class TestScheduleComputation:
    """§14.3：日任务按 Asia/Shanghai 本地时间计算下一次触发。"""

    def test_next_daily_same_day(self) -> None:
        scheduler = _scheduler()
        # 本地 20:00 → 次日 02:30
        nxt = scheduler._next_daily(NOW, time(2, 30))
        assert (
            nxt.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
            == "2026-08-12 02:30"
        )

    def test_next_daily_before_time_same_day(self) -> None:
        scheduler = _scheduler()
        now = datetime(2026, 8, 11, 18, 0, tzinfo=UTC)  # 本地 02:00（次日）
        nxt = scheduler._next_daily(now, time(2, 30))
        assert (
            nxt.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
            == "2026-08-12 02:30"
        )

    def test_interval_tasks_due_immediately(self, recorder: _SchedulerRecorder) -> None:
        scheduler = _scheduler()
        scheduler._ensure_initialized(NOW)
        interval_tasks = [t for t in scheduler_mod.TASKS if t.interval_seconds is not None]
        daily_tasks = [t for t in scheduler_mod.TASKS if t.daily_at is not None]
        for task in interval_tasks:
            assert scheduler._next_due[task.name] == NOW
        for task in daily_tasks:
            assert scheduler._next_due[task.name] > NOW


class TestLeaseRecovery:
    """§14.3：每 30 秒回收过期 operation / Outbox Lease。"""

    async def test_recover_operation_leases(self, recorder: _SchedulerRecorder) -> None:
        recorder.op_leases_recovered = 2
        has_more = await _scheduler().run_task("recover_operation_leases", NOW)
        assert has_more is False

    async def test_recover_outbox_leases(self, recorder: _SchedulerRecorder) -> None:
        recorder.outbox_leases_recovered = 1
        await _scheduler().run_task("recover_outbox_leases", NOW)
        # 无异常即调用了对应仓储（monkeypatch 计数由 fake 返回体现）

    async def test_tick_runs_interval_tasks_only(self, recorder: _SchedulerRecorder) -> None:
        ran = await _scheduler().tick(NOW)
        assert "recover_operation_leases" in ran
        assert "recover_outbox_leases" in ran
        assert "check_dead_letters" in ran
        # 日任务未到点不执行
        assert "purge_tombstones" not in ran
        assert "check_backup_runs" not in ran


class TestMaintenanceRunIdempotency:
    """§14.3：maintenance run 先创建或复用（带幂等键）。"""

    async def test_tombstone_run_reused_on_second_fire(self, recorder: _SchedulerRecorder) -> None:
        recorder.expired_tombstones = [{"user_id": uuid4(), "memory_id": "m"}]
        scheduler = _scheduler()
        await scheduler.run_task("purge_tombstones", NOW)
        await scheduler.run_task("purge_tombstones", NOW)
        keys = [k for k in recorder.runs if k.startswith("purge-tombstones:")]
        assert keys == ["purge-tombstones:2026-08-11"]  # 同日只创建一个 run
        # 第二次复用 run：operation 仍 queued → waiting，不再创建新 operation
        assert len(recorder.inserted_operations) == 1

    async def test_only_graph_batches_create_operations(self, recorder: _SchedulerRecorder) -> None:
        """只有进入 Graph 的 batch 才创建 memory_operations 并关联 operation_id。"""
        recorder.expired_tombstones = [{"user_id": uuid4(), "memory_id": "m"}]
        await _scheduler().run_task("purge_tombstones", NOW)
        assert len(recorder.inserted_operations) == 1
        operation = recorder.inserted_operations[0]
        assert operation.actor_type == "system"
        assert operation.user_id == SYSTEM_USER
        assert operation.input_kind == "maintenance"
        assert isinstance(operation.payload, MaintenanceCommand)
        assert operation.payload.kind == "purge_tombstones"
        # run 通过 operation_id 关联
        run = recorder.runs["purge-tombstones:2026-08-11"]
        assert run["operation_id"] == operation.operation_id

    async def test_no_work_creates_run_but_no_operation(self, recorder: _SchedulerRecorder) -> None:
        """无工作的日任务：run 幂等成功收尾，不创建空 operation。"""
        recorder.expired_tombstones = []
        await _scheduler().run_task("purge_tombstones", NOW)
        assert recorder.inserted_operations == []
        run = recorder.runs["purge-tombstones:2026-08-11"]
        assert run["status"] == "succeeded"
        assert recorder.completed_runs[0]["result"] == {"skipped": "no_expired_tombstones"}

    async def test_checkpoint_cleanup_no_work(self, recorder: _SchedulerRecorder) -> None:
        recorder.expired_checkpoints = []
        await _scheduler().run_task("cleanup_checkpoints", NOW)
        assert recorder.inserted_operations == []
        assert recorder.runs["cleanup-checkpoints:2026-08-11"]["status"] == "succeeded"

    async def test_failed_operation_marks_run_failed(self, recorder: _SchedulerRecorder) -> None:
        """operation 只表示 Graph batch 执行状态；终结失败时 run 置 failed。"""
        recorder.expired_tombstones = [{"user_id": uuid4(), "memory_id": "m"}]
        scheduler = _scheduler()
        await scheduler.run_task("purge_tombstones", NOW)
        run = recorder.runs["purge-tombstones:2026-08-11"]
        recorder.operations_by_id[run["operation_id"]]["status"] = "dead_letter"
        has_more = await scheduler.run_task("purge_tombstones", NOW)
        assert has_more is False
        assert run["status"] == "failed"

    async def test_continue_cursor_schedules_next_batch(self, recorder: _SchedulerRecorder) -> None:
        """graph 回写 running + cursor 后，Scheduler 调度下一批并换幂等键。"""
        recorder.expired_tombstones = [{"user_id": uuid4(), "memory_id": "m"}]
        scheduler = _scheduler()
        await scheduler.run_task("purge_tombstones", NOW)
        run = recorder.runs["purge-tombstones:2026-08-11"]
        recorder.operations_by_id[run["operation_id"]]["status"] = "succeeded"
        run["status"] = "running"
        run["cursor"] = "u1:m1"
        await scheduler.run_task("purge_tombstones", NOW)
        assert len(recorder.inserted_operations) == 2
        second = recorder.inserted_operations[1]
        assert second.payload.cursor == "u1:m1"
        assert second.idempotency_key.endswith(":u1:m1")


class TestNonGraphTasks:
    """§14.3：通知清理、备份检查不进入 Graph（不创建 memory_operations）。"""

    async def test_purge_notifications_creates_no_operation(
        self, recorder: _SchedulerRecorder
    ) -> None:
        recorder.notification_purge_result = 0
        await _scheduler().run_task("purge_notifications", NOW)
        assert recorder.inserted_operations == []
        run = recorder.runs["purge-notifications:2026-08-11"]
        assert run["status"] == "succeeded"
        # 90 天保留窗口
        cutoff = recorder.notification_purges[0]["cutoff"]
        assert NOW - cutoff == timedelta(days=90)

    async def test_backup_check_alert_without_success(
        self, recorder: _SchedulerRecorder, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        recorder.backup_ok = False
        with caplog.at_level(logging.ERROR, logger="memory.scheduler"):
            await _scheduler().run_task("check_backup_runs", NOW)
        assert recorder.inserted_operations == []
        assert any("告警" in record.message for record in caplog.records)
        run = recorder.runs["backup-check:2026-08-11"]
        assert run["status"] == "succeeded"

    async def test_backup_check_ok_no_alert(
        self, recorder: _SchedulerRecorder, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.ERROR, logger="memory.scheduler"):
            await _scheduler().run_task("check_backup_runs", NOW)
        assert caplog.records == []


class TestIndexRebuildScheduling:
    """§14.3：每 5 分钟调度 dirty index.md 重建。"""

    async def test_dirty_index_creates_run_and_operation(
        self, recorder: _SchedulerRecorder
    ) -> None:
        user_id = uuid4()
        dirty_at = datetime(2026, 8, 11, 11, 0, tzinfo=UTC)
        recorder.dirty_indexes = [{"user_id": user_id, "index_dirty_at": dirty_at}]
        await _scheduler().run_task("schedule_index_rebuilds", NOW)
        key = f"rebuild-index:{user_id}:{dirty_at.isoformat()}"
        assert key in recorder.runs
        assert len(recorder.inserted_operations) == 1
        operation = recorder.inserted_operations[0]
        assert operation.payload.kind == "rebuild_index"
        assert operation.payload.target_user_id == user_id

    async def test_no_dirty_index_no_run(self, recorder: _SchedulerRecorder) -> None:
        await _scheduler().run_task("schedule_index_rebuilds", NOW)
        assert recorder.runs == {}
        assert recorder.inserted_operations == []


class TestVerifyChecksumsScheduling:
    """§14.3 / 评审 #14：verify_checksums 每天 04:00 进入 TASKS 并按幂等 run 调度。"""

    def test_task_registered_at_0400(self) -> None:
        task = next(t for t in scheduler_mod.TASKS if t.name == "verify_checksums")
        assert task.daily_at == time(4, 0)
        assert task.interval_seconds is None

    async def test_no_active_documents_skips_operation(self, recorder: _SchedulerRecorder) -> None:
        recorder.active_documents = []
        has_more = await _scheduler().run_task("verify_checksums", NOW)
        assert has_more is False
        assert recorder.inserted_operations == []
        run = recorder.runs["verify-checksums:2026-08-11"]
        assert run["status"] == "succeeded"
        assert recorder.completed_runs[0]["result"] == {"skipped": "no_active_documents"}

    async def test_active_documents_create_graph_batch(self, recorder: _SchedulerRecorder) -> None:
        recorder.active_documents = [{"user_id": uuid4(), "memory_id": "learner"}]
        has_more = await _scheduler().run_task("verify_checksums", NOW)
        assert has_more is True
        assert len(recorder.inserted_operations) == 1
        operation = recorder.inserted_operations[0]
        assert operation.actor_type == "system"
        assert operation.user_id == SYSTEM_USER
        assert operation.input_kind == "maintenance"
        assert operation.operation_type == "verify_checksums"
        assert operation.payload.kind == "verify_checksums"
        run = recorder.runs["verify-checksums:2026-08-11"]
        assert run["operation_id"] == operation.operation_id
