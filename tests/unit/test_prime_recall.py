"""recall_memory 的 prime 模式单测（memory-rebuild §2.4 D1/D2，2026-09-11 裁决 A）。

裁决 A 的核心：**prime 每轮重新取回**，rollout 的 `memory_prime` 记录只做审计与变更检测
（因为它按 §1.5「大对象放引用」只存 hash + 条目数，读不回正文）。这里锁死三件事：

1. 非首轮也真的调用了 prime 接口，且注入给模型的 prime **非空**（回归：曾出现"读过 pin
   就当提示词用"，导致非首轮注入空 prime 且不报错）；
2. hash 未变 → 不重复写 pin 记录（避免每轮往 rollout 里堆重复记录）；
3. hash 变了 → 补一条 pin 记录（thread 中途换摘要这件事必须留痕）。
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from backend.conversation.graph.nodes.memory import recall_memory
from tests.conversation.graph_fixtures import build_runtime


class FakeMemoryGateway:
    """返回固定 prime 的记忆网关，并记录调用次数。"""

    def __init__(self, summary: str = "用户正在学圆锥曲线。") -> None:
        self.summary = summary
        self.prime_calls = 0

    async def build_memory_prime(self, *, user_id: str | None = None) -> dict[str, Any]:
        self.prime_calls += 1
        return {
            "summary": self.summary,
            "schema_version": "v1",
            "generated_at": "2026-09-10T05:00:00Z",
            "index_entries": [
                {
                    "memory_id": "mastery:椭圆",
                    "name": "椭圆",
                    "description": "圆锥曲线之一",
                    "keywords": [],
                }
            ],
            "summary_truncated": False,
            "degraded": False,
        }


class FakeRolloutReader:
    """按记录列表返回 thread 记录。"""

    def __init__(self, payloads: list[dict[str, Any]] | None = None) -> None:
        self.payloads = list(payloads or [])
        self.reads = 0

    async def read_thread_records(self, thread_id: Any) -> list[Any]:
        self.reads += 1
        return [_Record(payload) for payload in self.payloads]


class _Record:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.type = "memory_prime"
        self.payload = payload


class FakeRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    async def record(
        self, record_type: str, *, payload: dict[str, Any], turn_id: Any = None
    ) -> bool:
        self.records.append((record_type, payload))
        return True


def _state(*, first_turn: bool) -> dict[str, Any]:
    recent = [] if first_turn else [{"role": "user", "content": "上一轮问题"}]
    return {
        "user_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "turn_id": str(uuid4()),
        "request_id": "req-1",
        "run_id": "run-1",
        "conversation_context": {"recent_messages": recent},
    }


def _runtime(*, prime: dict[str, Any] | None, gateway: FakeMemoryGateway | None = None) -> Any:
    runtime = build_runtime(memory_gateway=gateway or FakeMemoryGateway())
    runtime.flags = {**runtime.flags, "memory_prime": True}
    runtime.rollout_recorder = FakeRecorder()
    if prime is not None:
        runtime.rollout_reader = FakeRolloutReader([prime])
    return runtime


def _pin_payload(summary: str, *, index_entry_count: int = 1) -> dict[str, Any]:
    import hashlib

    return {
        "summary_hash": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
        "schema_version": "v1",
        "generated_at": None,
        "truncated": False,
        "index_entry_count": index_entry_count,
    }


# ---------------------------------------------------------------------------
# 非首轮必须真的取回 prime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_first_turn_refetches_and_injects_non_empty_prime() -> None:
    gateway = FakeMemoryGateway()
    runtime = _runtime(prime=None, gateway=gateway)

    result = await recall_memory(_state(first_turn=False), runtime=runtime)

    assert gateway.prime_calls == 1
    prime = result["memory_prime"]
    assert prime["summary"] == "用户正在学圆锥曲线。"
    assert prime["index_entries"][0]["memory_id"] == "mastery:椭圆"
    assert result["memory_context"]["prime"]["summary"] == "用户正在学圆锥曲线。"
    assert result["memory_context"]["status"] == "available"


@pytest.mark.asyncio
async def test_non_first_turn_with_unchanged_hash_does_not_rewrite_pin() -> None:
    gateway = FakeMemoryGateway()
    runtime = _runtime(prime=_pin_payload("用户正在学圆锥曲线。"), gateway=gateway)

    await recall_memory(_state(first_turn=False), runtime=runtime)

    assert runtime.rollout_reader.reads == 1
    # hash 一致：不补写 pin 记录
    assert runtime.rollout_recorder.records == []


@pytest.mark.asyncio
async def test_non_first_turn_with_changed_summary_records_new_pin() -> None:
    gateway = FakeMemoryGateway(summary="用户已经掌握椭圆。")
    runtime = _runtime(prime=_pin_payload("用户正在学圆锥曲线。"), gateway=gateway)

    result = await recall_memory(_state(first_turn=False), runtime=runtime)

    records = runtime.rollout_recorder.records
    assert [record_type for record_type, _ in records] == ["memory_prime"]
    assert records[0][1]["index_entry_count"] == 1
    # 提示词用的是**最新**摘要，不是 pin 里的旧摘要
    assert result["memory_prime"]["summary"] == "用户已经掌握椭圆。"


@pytest.mark.asyncio
async def test_first_turn_writes_pin_once() -> None:
    runtime = _runtime(prime=None)

    await recall_memory(_state(first_turn=True), runtime=runtime)

    records = runtime.rollout_recorder.records
    assert [record_type for record_type, _ in records] == ["memory_prime"]
    payload = records[0][1]
    assert payload["schema_version"] == "v1"
    # 只落指纹与计数，不落摘要正文（§1.5 大对象放引用）
    assert "summary" not in payload
    assert payload["index_entry_count"] == 1


@pytest.mark.asyncio
async def test_missing_reader_is_not_a_degradation() -> None:
    """rollout 未启用（reader 为 None）时 pin 缺失不算降级：提示词照样是最新 prime。"""
    gateway = FakeMemoryGateway()
    runtime = _runtime(prime=None, gateway=gateway)
    runtime.rollout_reader = None

    result = await recall_memory(_state(first_turn=False), runtime=runtime)

    assert result["memory_prime"]["summary"] == "用户正在学圆锥曲线。"
    assert not result.get("degraded_flags")


@pytest.mark.asyncio
async def test_flag_off_keeps_legacy_path_untouched() -> None:
    """关闭 memory_prime 时不得调用 prime、不得写 pin、不得碰 rollout reader。"""

    class _LegacyGateway(FakeMemoryGateway):
        async def build_learning_context(self, **kwargs: Any) -> dict[str, Any]:
            return {"status": "available", "learner": {}, "truncated": False}

    legacy = _LegacyGateway()
    runtime = build_runtime(memory_gateway=legacy)
    runtime.flags = {**runtime.flags, "memory_prime": False, "memory_read": True}
    reader = FakeRolloutReader()
    runtime.rollout_reader = reader
    runtime.rollout_recorder = FakeRecorder()

    result = await recall_memory(_state(first_turn=False), runtime=runtime)

    assert legacy.prime_calls == 0
    assert reader.reads == 0
    assert runtime.rollout_recorder.records == []
    assert "memory_prime" not in result
