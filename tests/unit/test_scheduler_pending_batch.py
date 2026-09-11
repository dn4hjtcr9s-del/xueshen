"""Scheduler 证据池 nightly 批量入批的单元测试（memory-rebuild §2.6 / §5.8）。

仓储函数 monkeypatch 为内存实现（一个小的证据池模型 + run/operation 账本），覆盖：

- 门控 ``memory_batch_enabled`` 关闭时零交互（不建 run、不查询、无调度副作用）；
- 一个用户 3 条证据 → 1 个批次 operation、成员被 assign、cursor 推到最后一条；
- 同一 run 已有在途 operation → waiting，不重复建 operation；
- ``assign_batch_members`` 返回 0（整批被并发实例抢走）→ 不误报成功、作废刚建的 operation；
- 当天 run 已 succeeded → 跳过该用户；
- 续跑：cursor 推进后用 ``after`` 取下一批，已入批的成员不重复入批（不丢不重）；
- 触发时刻来自 config.summary_daily_time，而不是 TASKS 的声明式默认值。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.memory.contracts.batch import (
    BATCH_IDEMPOTENCY_KEY_TEMPLATE,
    BatchContractError,
    BatchCursor,
)
from backend.memory.contracts.commands import SummarizeUserMemoryBatchCommand
from backend.memory.persistence import maintenance as maintenance_repo
from backend.memory.persistence import operations as ops_repo
from backend.memory.worker import scheduler as scheduler_mod
from backend.memory.worker.scheduler import (
    Scheduler,
    SchedulerConfig,
    _batch_cursor_tuple,
    _batch_operation_key,
)
from tests.unit.worker_fakes import make_session_factory

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)  # 本地（Asia/Shanghai）2026-08-11 20:00
TODAY = "2026-08-11"


@dataclass
class _Evidence:
    """内存证据行：只保留入批扫描用到的列。"""

    operation_id: UUID
    user_id: UUID
    next_run_at: datetime
    created_at: datetime
    status: str = "pending_batch"
    batch_operation_id: UUID | None = None


@dataclass
class _BatchRecorder:
    """run / operation / 证据归属账本 + 调用记录。"""

    runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    evidence: list[_Evidence] = field(default_factory=list)
    operations: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    operations_by_key: dict[tuple[UUID, str, str], dict[str, Any]] = field(default_factory=dict)
    inserted: list[Any] = field(default_factory=list)
    attached: list[tuple[UUID, UUID]] = field(default_factory=list)
    completed_runs: list[dict[str, Any]] = field(default_factory=list)
    run_updates: list[dict[str, Any]] = field(default_factory=list)
    cancelled: list[UUID] = field(default_factory=list)
    #: 空列表表示"没有被调用过"；门控关闭测试断言它保持为空。
    calls: list[str] = field(default_factory=list)
    #: 非 None 时覆盖 assign_batch_members 的返回值（模拟并发抢走）。
    assign_rowcount: int | None = None
    #: 为 True 时成员查询返回空（模拟"读用户列表之后成员被并发领走"的窗口）。
    empty_members: bool = False
    #: 非 None 时 get_run_by_key 返回它（模拟本事务内重读看到并发实例刚写入的 run）。
    fresh_run: dict[str, Any] | None = None

    def add_evidence(
        self,
        *,
        user_id: UUID,
        count: int = 1,
        due_at: datetime = NOW,
        gate_offset: timedelta = timedelta(0),
    ) -> list[_Evidence]:
        """插入 count 条证据；``gate_offset`` 用来构造"未到点"的行。"""
        created = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
        rows = [
            _Evidence(
                operation_id=uuid4(),
                user_id=user_id,
                next_run_at=due_at + gate_offset,
                created_at=created + timedelta(seconds=index),
            )
            for index in range(count)
        ]
        self.evidence.extend(rows)
        return rows

    def add_run(
        self,
        *,
        user_id: UUID,
        status: str = "queued",
        cursor: str | None = None,
        operation_id: UUID | None = None,
        date: str = TODAY,
    ) -> dict[str, Any]:
        key = BATCH_IDEMPOTENCY_KEY_TEMPLATE.format(user_id=user_id, date=date)
        run = {
            "run_id": uuid4(),
            "maintenance_type": "summarize_pending_evidence",
            "idempotency_key": key,
            "status": status,
            "cursor": cursor,
            "operation_id": operation_id,
        }
        self.runs[key] = run
        return run

    def add_operation(self, *, operation_id: UUID, status: str, user_id: UUID, key: str) -> None:
        row = {"operation_id": operation_id, "status": status, "user_id": user_id}
        self.operations[operation_id] = row
        self.operations_by_key[(user_id, "system", key)] = row


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _BatchRecorder:
    rec = _BatchRecorder()

    async def fake_create_or_reuse_run(
        session: Any, *, run_id: Any, maintenance_type: str, idempotency_key: str
    ) -> tuple[dict[str, Any], bool]:
        rec.calls.append("create_or_reuse_run")
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
        rec.runs[idempotency_key] = run
        return run, True

    async def fake_get_run_by_key(session: Any, *, idempotency_key: str) -> dict[str, Any] | None:
        rec.calls.append("get_run_by_key")
        if rec.fresh_run is not None:
            return rec.fresh_run
        return rec.runs.get(idempotency_key)

    async def fake_attach_operation(session: Any, *, run_id: Any, operation_id: Any) -> None:
        rec.calls.append("attach_operation")
        rec.attached.append((run_id, operation_id))
        for run in rec.runs.values():
            if run["run_id"] == run_id:
                run["operation_id"] = operation_id

    async def fake_complete_run(session: Any, **kwargs: Any) -> None:
        rec.calls.append("complete_run")
        rec.completed_runs.append(kwargs)
        for run in rec.runs.values():
            if run["run_id"] == kwargs["run_id"]:
                run["status"] = kwargs["status"]
                run["cursor"] = kwargs["cursor"]

    async def fake_update_run_by_operation(session: Any, **kwargs: Any) -> None:
        rec.calls.append("update_run_by_operation")
        rec.run_updates.append(kwargs)
        for run in rec.runs.values():
            if run["operation_id"] == kwargs["operation_id"]:
                run["status"] = kwargs["status"]
                run["cursor"] = kwargs["cursor"]

    async def fake_list_open_runs(
        session: Any, *, maintenance_type: str, limit: int
    ) -> list[dict[str, Any]]:
        rec.calls.append("list_open_runs")
        rows = []
        for run in rec.runs.values():
            # fresh_run 模拟"并发实例刚写进库的状态"：sweep 读的是库里的真相，
            # 因此这里也必须看到它，否则会把在途批次误判成"该用户没有批次"。
            if rec.fresh_run is not None and run["run_id"] == rec.fresh_run.get("run_id"):
                run = rec.fresh_run
            if run["maintenance_type"] != maintenance_type or run["status"] != "running":
                continue
            rows.append(run)
        return rows[:limit]

    async def fake_list_user_ids(session: Any, *, now: datetime, limit: int) -> list[UUID]:
        rec.calls.append("list_pending_batch_user_ids")
        earliest: dict[UUID, datetime] = {}
        for row in rec.evidence:
            if row.status != "pending_batch" or row.batch_operation_id is not None:
                continue
            if row.next_run_at > now:
                continue
            if row.user_id not in earliest or row.next_run_at < earliest[row.user_id]:
                earliest[row.user_id] = row.next_run_at
        ordered = sorted(earliest.items(), key=lambda item: (item[1], item[0]))
        return [user_id for user_id, _ in ordered][:limit]

    async def fake_list_members(
        session: Any,
        *,
        user_id: UUID,
        now: datetime,
        limit: int,
        after: tuple[datetime, datetime, UUID] | None = None,
    ) -> list[dict[str, Any]]:
        rec.calls.append("list_pending_batch_members")
        if rec.empty_members:
            return []
        rows = [
            row
            for row in rec.evidence
            if row.user_id == user_id
            and row.status == "pending_batch"
            and row.batch_operation_id is None
            and row.next_run_at <= now
            and (after is None or (row.next_run_at, row.created_at, row.operation_id) > after)
        ]
        rows.sort(key=lambda row: (row.next_run_at, row.created_at, row.operation_id))
        return [
            {
                "operation_id": row.operation_id,
                "user_id": row.user_id,
                "next_run_at": row.next_run_at,
                "created_at": row.created_at,
            }
            for row in rows[:limit]
        ]

    async def fake_assign_members(
        session: Any, *, batch_operation_id: UUID, member_operation_ids: list[UUID]
    ) -> int:
        rec.calls.append("assign_batch_members")
        if rec.assign_rowcount is not None:
            return rec.assign_rowcount
        assigned = 0
        for row in rec.evidence:
            if (
                row.operation_id in set(member_operation_ids)
                and row.status == "pending_batch"
                and row.batch_operation_id is None
            ):
                row.batch_operation_id = batch_operation_id
                assigned += 1
        return assigned

    async def fake_insert_operation(session: Any, operation: Any, **kwargs: Any) -> bool:
        rec.calls.append("insert_operation")
        rec.inserted.append(operation)
        rec.add_operation(
            operation_id=operation.operation_id,
            status="queued",
            user_id=operation.user_id,
            key=operation.idempotency_key,
        )
        return True

    async def fake_get_by_idempotency(
        session: Any, *, user_id: Any, actor_type: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        rec.calls.append("get_by_idempotency")
        return rec.operations_by_key.get((user_id, actor_type, idempotency_key))

    async def fake_get_operation(session: Any, operation_id: UUID) -> dict[str, Any] | None:
        rec.calls.append("get_operation")
        return rec.operations.get(operation_id)

    async def fake_request_cancel(session: Any, *, operation_id: UUID) -> dict[str, Any] | None:
        rec.calls.append("request_cancel")
        rec.cancelled.append(operation_id)
        row = rec.operations.get(operation_id)
        if row is not None:
            row["status"] = "cancelled"
        return row

    monkeypatch.setattr(maintenance_repo, "create_or_reuse_run", fake_create_or_reuse_run)
    monkeypatch.setattr(maintenance_repo, "get_run_by_key", fake_get_run_by_key)
    monkeypatch.setattr(maintenance_repo, "attach_operation", fake_attach_operation)
    monkeypatch.setattr(maintenance_repo, "complete_run", fake_complete_run)
    monkeypatch.setattr(maintenance_repo, "update_run_by_operation", fake_update_run_by_operation)
    monkeypatch.setattr(maintenance_repo, "list_open_runs", fake_list_open_runs)
    monkeypatch.setattr(ops_repo, "list_pending_batch_user_ids", fake_list_user_ids)
    monkeypatch.setattr(ops_repo, "list_pending_batch_members", fake_list_members)
    monkeypatch.setattr(ops_repo, "assign_batch_members", fake_assign_members)
    monkeypatch.setattr(ops_repo, "insert_operation", fake_insert_operation)
    monkeypatch.setattr(ops_repo, "get_by_idempotency", fake_get_by_idempotency)
    monkeypatch.setattr(ops_repo, "get_operation", fake_get_operation)
    monkeypatch.setattr(ops_repo, "request_cancel", fake_request_cancel)
    return rec


def _scheduler(
    *, enabled: bool = True, max_evidence: int = 50, daily: time = time(0, 0)
) -> Scheduler:
    return Scheduler(
        session_factory=make_session_factory(),
        config=SchedulerConfig(
            evidence_batch_enabled=enabled,
            batch_max_evidence=max_evidence,
            summary_daily_time=daily,
        ),
        clock=lambda: NOW,
    )


class TestEvidenceBatchGate:
    """门控关闭时任务必须完全不可见（不建 run、不查询、无副作用）。"""

    async def test_disabled_flag_performs_no_database_work(self, recorder: _BatchRecorder) -> None:
        user_id = uuid4()
        recorder.add_evidence(user_id=user_id, count=3)
        has_more = await _scheduler(enabled=False).run_task("summarize_pending_evidence", NOW)
        assert has_more is False
        assert recorder.calls == [], "门控关闭时不得触碰任何仓储"
        assert recorder.runs == {}
        assert recorder.inserted == []

    def test_task_declared_and_daily_time_comes_from_config(self) -> None:
        task = next(t for t in scheduler_mod.TASKS if t.name == "summarize_pending_evidence")
        assert task.daily_at == time(0, 0)
        assert task.interval_seconds is None
        # 声明式默认值只是 TASKS 表里的静态值；真实触发时刻取 config
        scheduler = _scheduler(daily=time(1, 30))
        assert scheduler._daily_at_for(task) == time(1, 30)
        scheduler._ensure_initialized(NOW)
        assert scheduler._next_due["summarize_pending_evidence"] == scheduler._next_daily(
            NOW, time(1, 30)
        )
        # 其它日任务不受影响
        other = next(t for t in scheduler_mod.TASKS if t.name == "purge_tombstones")
        assert scheduler._daily_at_for(other) == time(3, 0)


class TestBatchCursorPureFunction:
    """cursor 解析是纯函数：空游标 → None，损坏游标 → 契约错误（不静默从头扫）。"""

    def test_empty_cursor_is_none(self) -> None:
        assert _batch_cursor_tuple(None) is None
        assert _batch_cursor_tuple("") is None

    def test_cursor_roundtrip(self) -> None:
        operation_id = uuid4()
        encoded = BatchCursor(
            eligible_at=NOW, created_at=NOW - timedelta(hours=1), operation_id=operation_id
        ).encode()
        assert _batch_cursor_tuple(encoded) == (NOW, NOW - timedelta(hours=1), operation_id)

    def test_corrupt_cursor_raises(self) -> None:
        with pytest.raises(BatchContractError):
            _batch_cursor_tuple("{not json")


class TestBatchOperationKey:
    """批次 operation 幂等键：cursor 的确定性函数，且不越 200 字符上限。"""

    def test_initial_cursor_uses_initial_suffix(self) -> None:
        run_key = f"summarize:{uuid4()}:2026-08-11"
        assert _batch_operation_key(run_key=run_key, cursor=None) == f"{run_key}:initial"

    def test_cursor_key_is_deterministic_bounded_and_distinct(self) -> None:
        run_key = f"summarize:{uuid4()}:2026-08-11"
        cursor = BatchCursor(eligible_at=NOW, created_at=NOW, operation_id=uuid4()).encode()
        first = _batch_operation_key(run_key=run_key, cursor=cursor)
        assert first == _batch_operation_key(run_key=run_key, cursor=cursor)
        assert first.startswith(f"{run_key}:")
        # 换 cursor（哪怕只差一个 operation_id）必须换键
        other = BatchCursor(eligible_at=NOW, created_at=NOW, operation_id=uuid4()).encode()
        assert _batch_operation_key(run_key=run_key, cursor=other) != first
        # 原样拼接 cursor 会越界（150+ 字符），摘要版加上 retry 后缀仍在上限内
        assert len(cursor) > 100
        assert len(f"{first}:retry-{uuid4().hex[:8]}") <= 200


class TestSingleUserBatch:
    """一个用户 3 条证据 → 1 个批次 operation、成员 assign、cursor 推到最后一条。"""

    async def test_three_evidence_create_one_batch_and_advance_cursor(
        self, recorder: _BatchRecorder
    ) -> None:
        user_id = uuid4()
        rows = recorder.add_evidence(user_id=user_id, count=3)

        has_more = await _scheduler().run_task("summarize_pending_evidence", NOW)

        assert has_more is True, "刚入批的 run 处于待续状态，应按 continuation 继续调度"
        assert len(recorder.inserted) == 1
        operation = recorder.inserted[0]
        payload = operation.payload
        assert isinstance(payload, SummarizeUserMemoryBatchCommand)
        assert payload.kind == "summarize_user_memory_batch"
        assert payload.target_user_id == user_id
        assert payload.batch_operation_id == operation.operation_id
        assert payload.max_evidence == 50
        assert operation.operation_type == "summarize_user_memory_batch"
        # 成员按稳定序（next_run_at, created_at, operation_id）入批
        assert [str(item) for item in payload.member_operation_ids] == [
            str(row.operation_id) for row in rows
        ]
        # 每条证据都被归属到该批次，且状态仍是 pending_batch（等批次终态回写）
        assert {row.batch_operation_id for row in rows} == {operation.operation_id}
        assert {row.status for row in rows} == {"pending_batch"}

        run = recorder.runs[BATCH_IDEMPOTENCY_KEY_TEMPLATE.format(user_id=user_id, date=TODAY)]
        assert run["operation_id"] == operation.operation_id
        assert run["status"] == "running"
        assert recorder.run_updates[0]["status"] == "running"
        cursor = BatchCursor.decode(run["cursor"])
        last = rows[-1]
        assert cursor.operation_id == last.operation_id
        assert cursor.eligible_at == last.next_run_at
        assert cursor.created_at == last.created_at

    async def test_batch_is_bounded_by_max_evidence_and_resumes_by_cursor(
        self, recorder: _BatchRecorder
    ) -> None:
        """上限 2、共 3 条：第一批 2 条；上一批成功后第二轮拿剩下 1 条，不丢不重。"""
        user_id = uuid4()
        rows = recorder.add_evidence(user_id=user_id, count=3)
        scheduler = _scheduler(max_evidence=2)

        assert await scheduler.run_task("summarize_pending_evidence", NOW) is True
        first = recorder.inserted[0]
        assert len(first.payload.member_operation_ids) == 2
        assert {row.batch_operation_id for row in rows[:2]} == {first.operation_id}
        assert rows[2].batch_operation_id is None, "超出上限的第 3 条本批不入批"

        # 模拟批量图把第一批跑完（operation 终态成功；run 由 Scheduler 维护为 running）
        recorder.operations[first.operation_id]["status"] = "succeeded"
        assert await scheduler.run_task("summarize_pending_evidence", NOW) is True

        assert len(recorder.inserted) == 2
        second = recorder.inserted[1]
        assert second.operation_id != first.operation_id
        assert [str(item) for item in second.payload.member_operation_ids] == [
            str(rows[2].operation_id)
        ], "续跑只拿 cursor 之后的未归属证据"
        assert rows[2].batch_operation_id == second.operation_id
        # 已入批的两条不得被第二个批次重复领走
        assert {row.batch_operation_id for row in rows[:2]} == {first.operation_id}


class TestRunStateShortCircuits:
    """run 侧短路：在途批次 waiting、当天已收尾跳过。"""

    async def test_inflight_operation_waits_without_new_batch(
        self, recorder: _BatchRecorder
    ) -> None:
        user_id = uuid4()
        pending = uuid4()
        run = recorder.add_run(user_id=user_id, status="running", operation_id=pending)
        recorder.add_operation(
            operation_id=pending, status="running", user_id=user_id, key="batch-inflight"
        )
        # 新到的证据让该用户仍出现在扫描结果里
        recorder.add_evidence(user_id=user_id, count=1)

        has_more = await _scheduler().run_task("summarize_pending_evidence", NOW)

        assert has_more is True, "在途批次未完成 → 按 continuation 继续调度"
        assert recorder.inserted == [], "不得重复建批次 operation"
        assert recorder.attached == []
        assert run["operation_id"] == pending
        assert run["cursor"] is None

    async def test_run_already_succeeded_skips_user(self, recorder: _BatchRecorder) -> None:
        user_id = uuid4()
        recorder.add_run(user_id=user_id, status="succeeded")
        recorder.add_evidence(user_id=user_id, count=2)
        recorder.empty_members = True

        has_more = await _scheduler().run_task("summarize_pending_evidence", NOW)

        assert has_more is False
        assert recorder.inserted == []
        assert recorder.run_updates == []

    async def test_empty_window_while_peer_batch_inflight_does_not_complete_run(
        self, recorder: _BatchRecorder
    ) -> None:
        """成员被别人领走且 run 上已有在途批次时，不得把 run 判成"无证据"提前收尾。"""
        user_id = uuid4()
        run = recorder.add_run(user_id=user_id, status="running")
        recorder.add_evidence(user_id=user_id, count=1)
        peer_batch = uuid4()
        recorder.add_operation(
            operation_id=peer_batch, status="queued", user_id=user_id, key="peer-batch"
        )
        # 本事务内重读 run 时看到并发实例刚挂上的在途批次
        recorder.fresh_run = {**run, "operation_id": peer_batch}
        recorder.empty_members = True

        has_more = await _scheduler().run_task("summarize_pending_evidence", NOW)

        assert has_more is True
        assert recorder.completed_runs == []

    async def test_truly_empty_window_completes_run(self, recorder: _BatchRecorder) -> None:
        user_id = uuid4()
        run = recorder.add_run(user_id=user_id, status="running")
        recorder.add_evidence(user_id=user_id, count=1)
        recorder.empty_members = True

        has_more = await _scheduler().run_task("summarize_pending_evidence", NOW)

        assert has_more is False
        assert [item["result"] for item in recorder.completed_runs] == [
            {"reason": "no_pending_evidence"}
        ]
        assert run["status"] == "succeeded"
        assert recorder.inserted == []


class TestConcurrentSteal:
    """assign 返回 0：整批成员被并发实例抢走，不得误报成功。"""

    async def test_stolen_batch_is_cancelled_and_not_attached(
        self, recorder: _BatchRecorder
    ) -> None:
        user_id = uuid4()
        rows = recorder.add_evidence(user_id=user_id, count=3)
        recorder.assign_rowcount = 0  # 模拟并发批次的其它幂等键先领走了整批

        has_more = await _scheduler().run_task("summarize_pending_evidence", NOW)

        assert has_more is False, "没有批次真正落地 → 不报待续"
        assert len(recorder.inserted) == 1, "先建了 operation，但随后作废"
        stolen = recorder.inserted[0]
        assert recorder.cancelled == [stolen.operation_id]
        assert recorder.operations[stolen.operation_id]["status"] == "cancelled"
        assert recorder.attached == [], "作废的 operation 不得挂到 run 上"
        assert recorder.run_updates == []
        run = recorder.runs[BATCH_IDEMPOTENCY_KEY_TEMPLATE.format(user_id=user_id, date=TODAY)]
        assert run["operation_id"] is None
        assert run["cursor"] is None, "成员没有真的入批，cursor 不得推进"
        assert run["status"] == "queued"
        assert {row.batch_operation_id for row in rows} == {None}


class TestBoundaryScanning:
    """门控与有界认领：未到点的证据不被扫描；超过用户上限的用户本轮不处理。"""

    async def test_not_yet_due_evidence_is_not_scanned(self, recorder: _BatchRecorder) -> None:
        due_user, early_user = uuid4(), uuid4()
        recorder.add_evidence(user_id=due_user, count=1)
        recorder.add_evidence(user_id=early_user, count=1, gate_offset=timedelta(hours=3))

        await _scheduler().run_task("summarize_pending_evidence", NOW)

        keys = set(recorder.runs)
        assert BATCH_IDEMPOTENCY_KEY_TEMPLATE.format(user_id=due_user, date=TODAY) in keys
        assert BATCH_IDEMPOTENCY_KEY_TEMPLATE.format(user_id=early_user, date=TODAY) not in keys

    async def test_max_users_per_run_bounds_the_scan(self, recorder: _BatchRecorder) -> None:
        for _ in range(3):
            recorder.add_evidence(user_id=uuid4(), count=1)
        scheduler = Scheduler(
            session_factory=make_session_factory(),
            config=SchedulerConfig(
                evidence_batch_enabled=True,
                batch_max_users_per_run=1,
                summary_daily_time=time(0, 0),
            ),
            clock=lambda: NOW,
        )

        await scheduler.run_task("summarize_pending_evidence", NOW)

        assert len(recorder.inserted) == 1
        assert len(recorder.runs) == 1
