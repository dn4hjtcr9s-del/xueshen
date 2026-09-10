"""Rollout 节点与 Runner 接线测试（memory-rebuild §5.3 Phase 1）。

单元recorder 测试覆盖"怎么写"，这里覆盖"**谁在什么时候写**"：
- snapshot 节点落 turn_context_snapshot（摘要级，不落正文）；
- runner 在 turn 边界开关段，段内顺序符合 §5.3 写入顺序；
- 图在 finalize 之前失败时补写 turn_completed(status=failed)；
- 未装配 recorder 时全部 no-op（flag 关闭行为不变）。
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.conversation.contracts.graph import (
    SnapshotBudgets,
    SnapshotMessage,
    TurnContextSnapshot,
)
from backend.conversation.graph.nodes.snapshot import build_turn_snapshot
from backend.conversation.graph.runner import ConversationGraphRunner
from backend.conversation.graph.state import (
    ConversationRuntimeContext,
    SystemClock,
    SystemIdGenerator,
)
from backend.conversation.rollout import codec as codec_module
from backend.conversation.rollout.recorder import RolloutRecorder

_CREATED = datetime(2026, 9, 10, 12, 0, 0, tzinfo=UTC)


class _FakeContextService:
    """最小 ContextService：返回固定快照，验证节点只落摘要级字段。"""

    def __init__(self, snapshot: TurnContextSnapshot) -> None:
        self._snapshot = snapshot

    def build_snapshot(self, **_kwargs: Any) -> TurnContextSnapshot:
        return self._snapshot


class _StubGraph:
    """可编程 compiled graph：ainvoke 时按需记录，或抛错。"""

    def __init__(self, on_invoke: Any = None, *, error: Exception | None = None) -> None:
        self._on_invoke = on_invoke
        self._error = error

    async def ainvoke(self, graph_input: Any, config: Any) -> None:
        runtime = config["configurable"]["runtime"]
        if self._on_invoke is not None:
            await self._on_invoke(runtime)
        if self._error is not None:
            raise self._error


def _runtime(recorder: RolloutRecorder | None) -> ConversationRuntimeContext:
    runtime = ConversationRuntimeContext(
        openai_gateway=None,
        memory_gateway=None,
        embedding_gateway=None,
        retriever_gateway=None,
        conversation_repository=None,
        turn_event_writer=None,
        clock=SystemClock(),
        id_generator=SystemIdGenerator(),
        logger=logging.getLogger("test.rollout.wiring"),
        flags={},
        worker_id="worker-1",
    )
    runtime.rollout_recorder = recorder
    return runtime


def _segment_files(root: Path) -> list[Path]:
    """同步辅助：收集段文件。

    ASYNC240 禁止在 async 函数内直接调用 pathlib 的阻塞方法；测试里把文件系统
    访问收敛到同步辅助中，既满足门禁，也让 async 测试体只表达断言。
    """
    return sorted(root.rglob("*.jsonl"))


def _all_entries(root: Path) -> list[Path]:
    """同步辅助：列出目录下全部条目。"""
    return sorted(root.rglob("*"))


def _read_records(path: Path) -> list[Any]:
    """同步辅助：读取并解析段内记录。"""
    return list(codec_module.iter_records(path.read_bytes()))


def _read_text(path: Path) -> str:
    """同步辅助：读取段文本（用于断言正文字段未落盘）。"""
    return path.read_text(encoding="utf-8")


def _recorder(tmp_path: Path) -> RolloutRecorder:
    return RolloutRecorder(
        root=tmp_path,
        clock=SystemClock(),
        id_generator=SystemIdGenerator(),
        logger=logging.getLogger("test.rollout.wiring"),
    )


def _turn(thread_id: uuid.UUID, turn_id: uuid.UUID, user_id: uuid.UUID) -> dict[str, Any]:
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "user_id": user_id,
        "user_message_id": uuid.uuid4(),
        "request_id": "req-1",
        "run_id": "run-1",
        "expected_thread_version": 1,
        "thread_created_at": _CREATED,
    }


async def _open(recorder: RolloutRecorder, turn: dict[str, Any]) -> None:
    await recorder.open_turn(
        thread_id=turn["thread_id"],
        turn_id=turn["turn_id"],
        user_id=turn["user_id"],
        thread_created_at=turn["thread_created_at"],
    )


# ---------------------------------------------------------------------------
# snapshot 节点
# ---------------------------------------------------------------------------


async def test_snapshot_node_records_summary_level_payload(tmp_path: Path) -> None:
    """§5.3 第 3 条：只落摘要级（hash + 消息 ID 序列 + token 数 + memory_status）。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    turn = _turn(thread_id, turn_id, user_id)
    await _open(recorder, turn)

    message_ids = [uuid.uuid4(), uuid.uuid4()]
    snapshot = TurnContextSnapshot(
        snapshot_id="snap-1",
        user_id=user_id,
        thread_id=thread_id,
        turn_id=turn_id,
        current_message="椭圆的焦点性质是什么",
        recent_messages=[
            SnapshotMessage(
                message_id=message_ids[0], role="user", sequence=1, content="椭圆的焦点性质是什么"
            ),
            SnapshotMessage(message_id=message_ids[1], role="assistant", sequence=2, content="……"),
        ],
        budgets=SnapshotBudgets(history_tokens=1234, memory_tokens=567),
        context_hash="hash-abc",
    )
    state = {
        "user_id": user_id,
        "thread_id": thread_id,
        "turn_id": turn_id,
        "conversation_context": {"current_message": "椭圆的焦点性质是什么"},
        "memory_context": {"status": "available"},
    }
    result = await build_turn_snapshot(
        state, runtime=_runtime(recorder), context_service=_FakeContextService(snapshot)
    )
    assert result["snapshot_hash"] == "hash-abc"
    await recorder.close_turn()
    await recorder.aclose()

    paths = _segment_files(tmp_path)
    assert len(paths) == 1
    records = _read_records(paths[0])
    types = [record.type for record in records]
    assert types == ["thread_meta", "turn_context_snapshot"]

    payload = records[1].payload
    assert payload["snapshot_hash"] == "hash-abc"
    assert payload["message_ids"] == [str(mid) for mid in message_ids]
    assert payload["token_estimate"] == 1801
    assert payload["memory_status"] == "available"
    # 摘要级：正文绝不出现
    assert "椭圆的焦点性质是什么" not in _read_text(paths[0])


# ---------------------------------------------------------------------------
# Runner 段生命周期
# ---------------------------------------------------------------------------


async def test_runner_writes_records_in_documented_order(tmp_path: Path) -> None:
    """§5.3 写入顺序：thread_meta → turn_started → … → turn_completed。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    turn = _turn(thread_id, turn_id, user_id)

    async def _on_invoke(runtime: ConversationRuntimeContext) -> None:
        from backend.conversation.rollout.recorder import record_rollout

        await record_rollout(
            runtime,
            "turn_started",
            {
                "turn_id": str(turn_id),
                "request_id": "req-1",
                "started_at": datetime.now(UTC).isoformat(),
            },
        )
        await record_rollout(
            runtime,
            "turn_completed",
            {
                "turn_id": str(turn_id),
                "status": "completed",
                "degraded_flags": ["retrieval_partial"],
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )

    runner = ConversationGraphRunner(
        compiled_graph=_StubGraph(_on_invoke),
        runtime_context=_runtime(recorder),
        graph_thread_id_for_turn=lambda tid: f"conv-turn:{tid}",
    )
    await runner.execute_turn(turn, worker_id="worker-1")
    await recorder.aclose()

    paths = _segment_files(tmp_path)
    assert len(paths) == 1
    records = _read_records(paths[0])
    assert [record.type for record in records] == [
        "thread_meta",
        "turn_started",
        "turn_completed",
    ]
    ordinals = [record.ordinal for record in records]
    assert ordinals == [0, 1, 2]
    assert records[-1].payload["degraded_flags"] == ["retrieval_partial"]
    # 段已关闭且无半行
    assert recorder.active_segment is None
    assert codec_module.has_truncated_tail(paths[0].read_bytes()) is False


async def test_runner_records_failed_turn_completed_when_graph_raises(tmp_path: Path) -> None:
    """图在 finalize 之前失败：补写 turn_completed(status=failed)，且异常照常抛出。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    turn = _turn(thread_id, turn_id, user_id)
    runner = ConversationGraphRunner(
        compiled_graph=_StubGraph(error=RuntimeError("模型不可用")),
        runtime_context=_runtime(recorder),
        graph_thread_id_for_turn=lambda tid: f"conv-turn:{tid}",
    )
    with pytest.raises(RuntimeError, match="模型不可用"):
        await runner.execute_turn(turn, worker_id="worker-1")
    await recorder.aclose()

    paths = _segment_files(tmp_path)
    assert len(paths) == 1
    records = _read_records(paths[0])
    assert [record.type for record in records] == ["thread_meta", "turn_completed"]
    assert records[-1].payload["status"] == "failed"
    assert "graph_failed" in records[-1].payload["degraded_flags"]


async def test_runner_does_not_record_second_turn_completed_after_finalize(
    tmp_path: Path,
) -> None:
    """finalize 已写 completed 后图再抛错：不得写成"完成又失败"。"""
    recorder = _recorder(tmp_path)
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    turn = _turn(thread_id, turn_id, user_id)

    async def _on_invoke(runtime: ConversationRuntimeContext) -> None:
        from backend.conversation.rollout.recorder import record_rollout

        await record_rollout(
            runtime,
            "turn_completed",
            {
                "turn_id": str(turn_id),
                "status": "completed",
                "degraded_flags": [],
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )

    runner = ConversationGraphRunner(
        compiled_graph=_StubGraph(_on_invoke, error=RuntimeError("finalize 之后的故障")),
        runtime_context=_runtime(recorder),
        graph_thread_id_for_turn=lambda tid: f"conv-turn:{tid}",
    )
    with pytest.raises(RuntimeError):
        await runner.execute_turn(turn, worker_id="worker-1")
    await recorder.aclose()

    paths = _segment_files(tmp_path)
    records = _read_records(paths[0])
    assert [record.type for record in records].count("turn_completed") == 1
    assert records[-1].payload["status"] == "completed"


async def test_runner_without_recorder_creates_no_files(tmp_path: Path) -> None:
    """flag 关闭（recorder 为 None）：runner 照常执行且不产生任何文件。"""
    runner = ConversationGraphRunner(
        compiled_graph=_StubGraph(None),
        runtime_context=_runtime(None),
        graph_thread_id_for_turn=lambda tid: f"conv-turn:{tid}",
    )
    turn = _turn(uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    await runner.execute_turn(turn, worker_id="worker-1")
    assert _all_entries(tmp_path) == []
