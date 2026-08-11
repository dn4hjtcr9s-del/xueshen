"""Outbox Consumer 路由单元测试（§14.4 / §13.12 / §23.1）。

仓储与映射/证据查询均 monkeypatch 为内存实现。覆盖：
空候选不建 operation、三类事件 projection_action/aggregate_version、
幂等键格式、单 target 失败隔离、全成功才 published、dead_letter 告警。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.memory.contracts.commands import ProjectSummaryToGraphCommand
from backend.memory.contracts.evidence import GraphProjectionEvidence
from backend.memory.persistence import notifications as notifications_repo
from backend.memory.persistence import operations as ops_repo
from backend.memory.persistence import outbox as outbox_repo
from backend.memory.worker import outbox_consumer as consumer_mod
from backend.memory.worker.outbox_consumer import OutboxConsumer
from tests.unit.worker_fakes import make_session_factory

USER = UUID("00000000-0000-4000-8000-000000000077")


def _row(event_type: str, payload: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "outbox_id": uuid4(),
        "user_id": USER,
        "event_type": event_type,
        "aggregate_type": "memory",
        "aggregate_id": str(payload.get("memory_id", "unknown")),
        "aggregate_version": payload.get("after_version") or payload.get("deleted_version") or 0,
        "payload": payload,
        "attempt_count": 1,
        "max_attempts": 10,
    }
    row.update(kwargs)
    return row


def _delivery(target: str, **kwargs: Any) -> dict[str, Any]:
    delivery: dict[str, Any] = {
        "delivery_id": uuid4(),
        "target": target,
        "status": "pending",
        "idempotency_key": f"key-{target}",
        "attempt_count": 0,
    }
    delivery.update(kwargs)
    return delivery


class _ConsumerRecorder:
    def __init__(self) -> None:
        self.inserted_operations: list[Any] = []
        self.insert_operation_result: bool = True
        self.marked: list[tuple[str, str]] = []
        self.event_logs: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self.finalized: list[Any] = []
        self.rescheduled: list[dict[str, Any]] = []
        self.outbox_status: str = "published"
        self.deliveries: list[dict[str, Any]] = []
        self.notification_error: Exception | None = None


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _ConsumerRecorder:
    rec = _ConsumerRecorder()

    async def fake_insert_operation(session: Any, operation: Any, **kwargs: Any) -> bool:
        if rec.insert_operation_result:
            rec.inserted_operations.append(operation)
        return rec.insert_operation_result

    async def fake_mark_delivery(session: Any, *, delivery_id: Any, status: str, **kw: Any) -> None:
        rec.marked.append((str(delivery_id), status))

    async def fake_event_log(session: Any, **kwargs: Any) -> bool:
        rec.event_logs.append(kwargs)
        return True

    async def fake_notification(session: Any, **kwargs: Any) -> None:
        if rec.notification_error is not None:
            raise rec.notification_error
        rec.notifications.append(kwargs)

    async def fake_finalize(session: Any, *, outbox_id: Any) -> None:
        rec.finalized.append(outbox_id)

    async def fake_reschedule(session: Any, **kwargs: Any) -> None:
        rec.rescheduled.append(kwargs)

    async def fake_get_status(session: Any, *, outbox_id: Any) -> str:
        return rec.outbox_status

    async def fake_list_deliveries(session: Any, *, outbox_id: Any) -> list[dict[str, Any]]:
        return rec.deliveries

    async def fake_link(session: Any, **kwargs: Any) -> dict[str, Any]:
        return {"mapping_method": "exact_alias", "mapping_confidence": 0.95}

    async def fake_evidence(session: Any, **kwargs: Any) -> list[GraphProjectionEvidence]:
        return [
            GraphProjectionEvidence(
                evidence_ref="conv:t1:m1",
                direction="learning",
                strength=0.6,
                occurred_at=datetime.now(UTC),
            )
        ]

    monkeypatch.setattr(ops_repo, "insert_operation", fake_insert_operation)
    monkeypatch.setattr(outbox_repo, "mark_delivery", fake_mark_delivery)
    monkeypatch.setattr(outbox_repo, "insert_internal_event_log", fake_event_log)
    monkeypatch.setattr(outbox_repo, "finalize_outbox", fake_finalize)
    monkeypatch.setattr(outbox_repo, "reschedule_outbox", fake_reschedule)
    monkeypatch.setattr(outbox_repo, "get_status", fake_get_status)
    monkeypatch.setattr(outbox_repo, "list_deliveries", fake_list_deliveries)
    monkeypatch.setattr(notifications_repo, "insert_notification", fake_notification)
    monkeypatch.setattr(consumer_mod, "load_projection_link", fake_link)
    monkeypatch.setattr(consumer_mod, "load_commit_evidence", fake_evidence)
    return rec


def _consumer() -> OutboxConsumer:
    return OutboxConsumer(session_factory=make_session_factory())


async def test_memory_changed_creates_apply_active_version_operations(
    recorder: _ConsumerRecorder,
) -> None:
    """§14.4：memory.changed → 每候选节点 apply_active_version，版本取 after_version。"""
    row = _row(
        "memory.changed",
        {
            "memory_id": "mastery:t1",
            "after_version": 3,
            "graph_projection_candidates": ["n001", "n002"],
        },
    )
    consumer = _consumer()
    await consumer._deliver_summary_projection(make_session_factory()(), row)
    assert len(recorder.inserted_operations) == 2
    by_node = {op.payload.node_id: op for op in recorder.inserted_operations}
    assert set(by_node) == {"n001", "n002"}
    op = by_node["n001"]
    command = op.payload
    assert isinstance(command, ProjectSummaryToGraphCommand)
    assert command.projection_action == "apply_active_version"
    assert command.source_version == 3  # aggregate_version=after_version
    assert command.mapping_method == "exact_alias"
    assert op.actor_type == "summary_projection"
    assert op.input_kind == "projection"
    # §14.4 幂等键格式
    assert op.idempotency_key == "summary-projection:mastery:t1:3:n001"
    assert by_node["n002"].idempotency_key == "summary-projection:mastery:t1:3:n002"


async def test_memory_deleted_uses_recompute_with_deleted_version(
    recorder: _ConsumerRecorder,
) -> None:
    """§14.4：memory.deleted → recompute_without_deleted_version，

    aggregate_version=deleted_version，绝不把删除版本当活动版本。
    """
    row = _row(
        "memory.deleted",
        {
            "memory_id": "mastery:t2",
            "deleted_version": 2,
            "graph_projection_candidates": ["n007"],
        },
    )
    await _consumer()._deliver_summary_projection(make_session_factory()(), row)
    assert len(recorder.inserted_operations) == 1
    op = recorder.inserted_operations[0]
    assert op.payload.projection_action == "recompute_without_deleted_version"
    assert op.payload.source_version == 2
    assert op.idempotency_key == "summary-projection:mastery:t2:2:n007"


async def test_memory_restored_uses_apply_active_version(
    recorder: _ConsumerRecorder,
) -> None:
    """§14.4：memory.restored → apply_active_version，aggregate_version=after_version。"""
    row = _row(
        "memory.restored",
        {
            "memory_id": "mastery:t3",
            "after_version": 5,
            "graph_projection_candidates": ["n010"],
        },
    )
    await _consumer()._deliver_summary_projection(make_session_factory()(), row)
    op = recorder.inserted_operations[0]
    assert op.payload.projection_action == "apply_active_version"
    assert op.payload.source_version == 5


async def test_empty_candidates_create_no_operation(recorder: _ConsumerRecorder) -> None:
    """§14.4：graph_projection_candidates 为空时直接幂等成功，不创建空 operation。"""
    row = _row(
        "memory.changed",
        {"memory_id": "mastery:t4", "after_version": 1, "graph_projection_candidates": []},
    )
    await _consumer()._deliver_summary_projection(make_session_factory()(), row)
    assert recorder.inserted_operations == []


async def test_learner_event_without_candidates_is_noop(recorder: _ConsumerRecorder) -> None:
    """§14.4：learner 事件无图谱候选，不强制关联。"""
    row = _row("learner.updated", {"memory_id": "learner", "after_version": 2})
    await _consumer()._deliver_summary_projection(make_session_factory()(), row)
    assert recorder.inserted_operations == []


async def test_duplicate_operation_insert_is_idempotent_success(
    recorder: _ConsumerRecorder,
) -> None:
    """至少一次投递下 operation 已存在：幂等成功，不抛错。"""
    recorder.insert_operation_result = False
    row = _row(
        "memory.changed",
        {"memory_id": "mastery:t5", "after_version": 1, "graph_projection_candidates": ["n001"]},
    )
    await _consumer()._deliver_summary_projection(make_session_factory()(), row)


async def test_single_target_failure_isolated(recorder: _ConsumerRecorder) -> None:
    """§13.12：单 target 失败只置该 delivery retry_wait，不影响其他 target；
    未全部成功时主行 retry_wait 并按指数退避重排。"""
    rec = recorder
    rec.notification_error = RuntimeError("通知写入失败")
    rec.outbox_status = "retry_wait"
    notification_delivery = _delivery("user_notification")
    projection_delivery = _delivery("summary_projection")
    log_delivery = _delivery("internal_event_log")
    rec.deliveries = [log_delivery, notification_delivery, projection_delivery]
    row = _row(
        "memory.deleted",
        {
            "memory_id": "mastery:t6",
            "deleted_version": 1,
            "restore_until": datetime.now(UTC).isoformat(),
            "graph_projection_candidates": [],
        },
    )
    await _consumer()._process(row)
    marked = {delivery_id: status for delivery_id, status in rec.marked}
    assert marked[str(log_delivery["delivery_id"])] == "succeeded"
    assert marked[str(projection_delivery["delivery_id"])] == "succeeded"
    assert marked[str(notification_delivery["delivery_id"])] == "retry_wait"
    # 主行未 published：退避重排而非 finalize 后直接完成
    assert len(rec.finalized) == 1
    assert len(rec.rescheduled) == 1
    assert rec.rescheduled[0]["next_run_at"] > datetime.now(UTC)
    # internal_event_log 使用 delivery 幂等键
    assert rec.event_logs[0]["idempotency_key"] == "key-internal_event_log"


async def test_all_targets_succeeded_publishes_without_reschedule(
    recorder: _ConsumerRecorder,
) -> None:
    """§13.12：所有启用 target 成功后主行 published，不再重排。"""
    recorder.outbox_status = "published"
    recorder.deliveries = [_delivery("internal_event_log")]
    row = _row("learner.updated", {"memory_id": "learner", "after_version": 2})
    await _consumer()._process(row)
    assert recorder.rescheduled == []
    assert len(recorder.finalized) == 1


async def test_delivery_dead_letter_after_max_attempts(
    recorder: _ConsumerRecorder, caplog: pytest.LogCaptureFixture
) -> None:
    """§14.4/§13.12：最多 10 次重试后 delivery dead_letter，主行 dead_letter 并告警。"""
    recorder.notification_error = RuntimeError("持续失败")
    recorder.outbox_status = "dead_letter"
    failing = _delivery("user_notification", attempt_count=9)
    recorder.deliveries = [failing]
    row = _row(
        "memory.restored",
        {"memory_id": "mastery:t7", "after_version": 4, "graph_projection_candidates": []},
    )
    with caplog.at_level(logging.ERROR, logger="memory.outbox_consumer"):
        await _consumer()._process(row)
    assert recorder.marked == [(str(failing["delivery_id"]), "dead_letter")]
    assert any("告警" in record.message for record in caplog.records)


async def test_internal_event_log_uses_delivery_idempotency_key(
    recorder: _ConsumerRecorder,
) -> None:
    """§13.12：internal_event_log 以唯一幂等键写入。"""
    recorder.deliveries = [_delivery("internal_event_log")]
    row = _row("graph_state.changed", {"node_id": "n001"}, aggregate_type="graph_node")
    await _consumer()._process(row)
    assert len(recorder.event_logs) == 1
    assert recorder.event_logs[0]["idempotency_key"] == "key-internal_event_log"
    assert recorder.event_logs[0]["event_type"] == "graph_state.changed"
