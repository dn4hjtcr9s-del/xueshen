"""Worker 执行路径单元测试（§14.1 / §11.5 / §11.6 / §23.1）。

仓储函数 monkeypatch 为内存实现；runner 为 fake。覆盖：
取消在 commit 前生效、失败分类 → retry_wait/needs_review/dead_letter、
hard timeout 保持 running 等 Scheduler 回收。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.memory.contracts.commands import MaintenanceCommand
from backend.memory.contracts.errors import (
    MemoryNotFoundError,
    MemoryVersionConflictError,
    OpenAITimeoutError,
)
from backend.memory.contracts.operations import MemoryOperation, MemoryOperationResult
from backend.memory.persistence import operations as ops_repo
from backend.memory.worker.worker import Worker, WorkerConfig
from tests.unit.worker_fakes import make_session_factory

USER = UUID("00000000-0000-4000-8000-000000000099")


def _row(*, attempt_count: int = 1, max_attempts: int = 3) -> dict[str, Any]:
    operation = MemoryOperation(
        operation_id=uuid4(),
        idempotency_key=f"idem-{uuid4().hex[:8]}",
        user_id=USER,
        actor_type="system",
        input_kind="maintenance",
        operation_type="purge_tombstones",
        priority=0,
        occurred_at=datetime.now(UTC),
        payload=MaintenanceCommand(kind="purge_tombstones"),
        trace_id=uuid4().hex + uuid4().hex,
        graph_thread_id=f"memory-op:{uuid4()}",
    )
    row = operation.model_dump(mode="json")
    row["attempt_count"] = attempt_count
    row["max_attempts"] = max_attempts
    return row


def _result(row: dict[str, Any], status: str = "succeeded") -> MemoryOperationResult:
    now = datetime.now(UTC)
    return MemoryOperationResult(
        operation_id=UUID(row["operation_id"]),
        status=status,  # type: ignore[arg-type]
        operation_type=row["operation_type"],
        created_at=now,
        updated_at=now,
        completed_at=now,
    )


class _FakeRunner:
    def __init__(self, *, exc: BaseException | None = None, delay: float = 0.0) -> None:
        self.exc = exc
        self.delay = delay
        self.calls: list[MemoryOperation] = []

    async def run(self, operation: MemoryOperation) -> MemoryOperationResult:
        self.calls.append(operation)
        if self.delay:
            import asyncio

            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        return _result(
            {
                "operation_id": str(operation.operation_id),
                "operation_type": operation.operation_type,
            }
        )


class _Recorder:
    """ops_repo 写函数的调用记录。"""

    def __init__(self) -> None:
        self.completed: list[dict[str, Any]] = []
        self.rescheduled: list[dict[str, Any]] = []
        self.cleared_commit_marker: list[Any] = []
        self.cancel_requested: bool = False


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    rec = _Recorder()

    async def fake_complete(session: Any, **kwargs: Any) -> None:
        rec.completed.append(kwargs)

    async def fake_reschedule(session: Any, **kwargs: Any) -> None:
        rec.rescheduled.append(kwargs)

    async def fake_cancel_requested(session: Any, operation_id: Any) -> bool:
        return rec.cancel_requested

    async def fake_clear_commit_started(session: Any, *, operation_id: Any) -> None:
        rec.cleared_commit_marker.append(operation_id)

    monkeypatch.setattr(ops_repo, "complete_operation", fake_complete)
    monkeypatch.setattr(ops_repo, "reschedule_operation", fake_reschedule)
    monkeypatch.setattr(ops_repo, "get_cancel_requested", fake_cancel_requested)
    monkeypatch.setattr(ops_repo, "clear_commit_started", fake_clear_commit_started)
    return rec


def _worker(runner: _FakeRunner, **config: Any) -> Worker:
    config.setdefault("heartbeat_interval_seconds", 9999.0)
    return Worker(
        session_factory=make_session_factory(),
        runner=runner,
        config=WorkerConfig(**config),
    )


async def test_cancel_before_commit_takes_effect(recorder: _Recorder) -> None:
    """§11.6：running 且未进入 commit 副作用时协作取消，在 commit 前生效。"""
    recorder.cancel_requested = True
    runner = _FakeRunner()
    await _worker(runner)._execute(_row())
    assert runner.calls == []  # 未执行 Graph
    assert [c["status"] for c in recorder.completed] == ["cancelled"]


async def test_execute_clears_stale_commit_marker_first(recorder: _Recorder) -> None:
    """§11.6（裁决 2026-08-11）：崩溃残留的 commit_started_at 在执行开始时清除。"""
    row = _row()
    runner = _FakeRunner()
    await _worker(runner)._execute(row)
    assert recorder.cleared_commit_marker == [row["operation_id"]]
    assert len(runner.calls) == 1


async def test_success_completes_with_result(recorder: _Recorder) -> None:
    runner = _FakeRunner()
    await _worker(runner)._execute(_row())
    assert len(runner.calls) == 1
    assert [c["status"] for c in recorder.completed] == ["succeeded"]


async def test_version_conflict_goes_needs_review(recorder: _Recorder) -> None:
    """§11.2/§11.3：可由用户处理的版本冲突 → needs_review。"""
    runner = _FakeRunner(exc=MemoryVersionConflictError("版本冲突"))
    await _worker(runner)._execute(_row())
    assert [c["status"] for c in recorder.completed] == ["needs_review"]
    assert recorder.completed[0]["public_error"]["code"] == "MEMORY_VERSION_CONFLICT"


async def test_permanent_error_goes_dead_letter(recorder: _Recorder) -> None:
    """§11.1：目标不存在等永久错误不重试，直接 dead_letter。"""
    runner = _FakeRunner(exc=MemoryNotFoundError("不存在"))
    await _worker(runner)._execute(_row())
    assert [c["status"] for c in recorder.completed] == ["dead_letter"]
    assert recorder.rescheduled == []


async def test_retryable_error_reschedules_retry_wait(recorder: _Recorder) -> None:
    """§11.2：临时错误退避后 retry_wait。"""
    runner = _FakeRunner(exc=OpenAITimeoutError("超时"))
    await _worker(runner)._execute(_row())
    assert recorder.completed == []
    assert len(recorder.rescheduled) == 1
    call = recorder.rescheduled[0]
    assert call["status"] == "retry_wait"
    assert call["next_run_at"] > datetime.now(UTC)


async def test_max_attempts_exhausted_goes_dead_letter(recorder: _Recorder) -> None:
    """§11.2：attempt 达到 max_attempts 后临时错误也转 dead_letter。"""
    runner = _FakeRunner(exc=OpenAITimeoutError("超时"))
    await _worker(runner)._execute(_row(attempt_count=3, max_attempts=3))
    assert [c["status"] for c in recorder.completed] == ["dead_letter"]
    assert recorder.rescheduled == []


async def test_hard_timeout_stays_running_for_lease_recovery(recorder: _Recorder) -> None:
    """§11.5：hard timeout 取消协程并停止续约；保持 running 等 Lease 过期回收。"""
    runner = _FakeRunner(delay=60.0)
    await _worker(runner, hard_timeout_seconds=0.05, soft_timeout_seconds=0.01)._execute(_row())
    # 不 complete、不 reschedule：行保持 running，由 Scheduler 回收
    assert recorder.completed == []
    assert recorder.rescheduled == []


async def test_claim_batch_limited_to_free_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """评审 #7：领取数量不超过空闲并发槽位——在 semaphore 外等待的任务
    没有心跳，Lease 过期会被 Scheduler 回收并二次领取。"""
    import asyncio

    calls: list[int] = []

    async def fake_claim(session: Any, **kwargs: Any) -> list[Any]:
        calls.append(int(kwargs["batch_size"]))
        return []

    monkeypatch.setattr(ops_repo, "claim_operation", fake_claim)
    worker = _worker(_FakeRunner(), concurrency=2, batch_size=10)

    # 空载：按空闲槽位（2）领取，而不是 batch_size（10）
    assert await worker._claim_batch() == []
    assert calls == [2]

    # 槽位占满：不再领取，也不调用仓储
    tasks = {asyncio.create_task(asyncio.sleep(60)) for _ in range(2)}
    worker._tasks.update(tasks)
    try:
        assert await worker._claim_batch() == []
        assert calls == [2]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
