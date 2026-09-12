"""prime 注入的有界性测试（review I-5）。

登记 ADD-047 曾声称「条数上限交由 conversation 侧的 token 预算裁剪负责」，但那段代码
**不存在**：服务端全量返回注册表目录，节点把整个 dict 原样塞进快照与 answer 视图。
这里锁死修复后的三条不变量：

1. `trim_prime_to_budget`：超预算时保留**前缀**、置 `index_entries_truncated`、不改原 dict；
   未配置预算（<=0）或没有 token 计数器时不裁剪（与 memory 工具结果同一语义）；
2. 节点侧（`recall_memory`）：注入给模型/checkpoint 的目录有界，且**不静默丢内容**——
   走既有 `turn.degraded` 机制发 `memory_prime_degraded`（flag 在封闭 `DegradedFlag` 里），
   快照 status 转 `degraded`；pin 记录里的条目数是**实际注入**的条数；
3. 快照与 answer 视图：即使快照里的 prime 未裁剪（修复前写入的 checkpoint），
   视图出口仍必须是有界的。
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from backend.conversation.contracts.events import TurnDegradedPayload
from backend.conversation.contracts.graph import SnapshotMemory, TurnContextSnapshot
from backend.conversation.graph.nodes.memory import recall_memory
from backend.conversation.services.context_service import ContextService, trim_prime_to_budget
from backend.conversation.services.token_counter import TokenCounter, WhitespaceTokenizer
from backend.settings import Settings
from tests.conversation.graph_fixtures import build_runtime

#: 测试预算：远小于下面构造的目录体积，保证一定触发裁剪
BUDGET = 120

ENTRY_COUNT = 40


def _counter() -> TokenCounter:
    return TokenCounter(tokenizer=WhitespaceTokenizer())


def _entries(count: int) -> list[dict[str, Any]]:
    return [
        {
            "memory_id": f"mastery:{index:04d}",
            "name": f"主题{index}",
            "description": "圆锥曲线的统一定义与焦点性质",
            "keywords": ["关键词", "别名"],
        }
        for index in range(count)
    ]


def _prime(count: int, *, summary: str = "## 用户画像\n偏好例题驱动") -> dict[str, Any]:
    return {
        "summary": summary,
        "schema_version": "v1",
        "generated_at": "2026-09-10T05:00:00Z",
        "index_entries": _entries(count),
        "summary_truncated": False,
        "degraded": False,
        "index_entries_truncated": False,
    }


def _injected_tokens(prime: dict[str, Any], counter: TokenCounter) -> int:
    """注入给模型的 token 量：summary + 目录条目（与服务端/节点的口径一致）。"""
    used = counter.count(str(prime.get("summary") or ""))
    for entry in prime.get("index_entries") or []:
        used += counter.count(json.dumps(entry, ensure_ascii=False, sort_keys=True))
    return used


# ---------------------------------------------------------------------------
# 纯函数：trim_prime_to_budget
# ---------------------------------------------------------------------------


def test_trim_prime_keeps_prefix_within_budget_and_marks_truncation() -> None:
    counter = _counter()
    prime = _prime(ENTRY_COUNT)

    trimmed, truncated = trim_prime_to_budget(prime, budget_tokens=BUDGET, token_counter=counter)

    assert truncated is True
    kept = trimmed["index_entries"]
    assert 0 < len(kept) < ENTRY_COUNT
    assert trimmed["index_entries_truncated"] is True
    # 前缀语义：保留的是服务端固定序的前 N 条，不是任意子集
    assert kept == prime["index_entries"][: len(kept)]
    assert trimmed["summary"] == prime["summary"]
    # 实际注入量确实落在预算内
    assert _injected_tokens(trimmed, counter) <= BUDGET
    # 不就地修改入参（gateway 返回的 dict 可能被别的调用方复用）
    assert len(prime["index_entries"]) == ENTRY_COUNT
    assert prime["index_entries_truncated"] is False


def test_trim_prime_is_noop_when_within_budget_or_budget_disabled() -> None:
    counter = _counter()
    small = _prime(2)
    assert trim_prime_to_budget(small, budget_tokens=BUDGET, token_counter=counter) == (
        small,
        False,
    )
    big = _prime(ENTRY_COUNT)
    # 未配置预算（<=0）与没有 token 计数器：与 memory_tool 的裁剪语义一致，不裁剪
    assert trim_prime_to_budget(big, budget_tokens=0, token_counter=counter)[1] is False
    assert trim_prime_to_budget(big, budget_tokens=BUDGET, token_counter=None)[1] is False


def test_trim_prime_counts_summary_into_the_same_budget() -> None:
    """新发现 10：summary 与目录**共用**同一预算，两者总量不得超预算。

    旧实现只裁目录、summary 完全不计入，于是"主题多 + 摘要长"的用户仍能把两份内容都
    塞进首轮提示词，快照里申报的 ``budgets.memory_tokens`` 与实际注入量不一致。
    """
    counter = _counter()
    summary = " ".join(["画像"] * 20)  # 20 token 的摘要
    prime = _prime(ENTRY_COUNT, summary=summary)

    trimmed, truncated = trim_prime_to_budget(prime, budget_tokens=BUDGET, token_counter=counter)

    assert truncated is True
    assert trimmed["summary"] == summary, "预算够放 summary 时不得截断它"
    assert _injected_tokens(trimmed, counter) <= BUDGET
    # 目录被压到 summary 之后剩余的预算里（而不是各拿一份 BUDGET）
    kept = trimmed["index_entries"]
    assert 0 < len(kept) < ENTRY_COUNT
    assert trimmed["index_entries_truncated"] is True


def test_trim_prime_truncates_summary_when_it_alone_exceeds_budget() -> None:
    """summary 单独超预算：先截断 summary（置 summary_truncated）、目录裁到 0 条。"""
    counter = _counter()
    long_summary = " ".join(["画像描述"] * 200)
    prime = _prime(ENTRY_COUNT, summary=long_summary)

    trimmed, truncated = trim_prime_to_budget(prime, budget_tokens=BUDGET, token_counter=counter)

    assert truncated is True
    assert trimmed["summary_truncated"] is True, "summary 被截断必须置标记（不静默丢内容）"
    assert trimmed["summary"] != long_summary
    assert trimmed["summary"].endswith("…"), "截断后的摘要应有省略标记"
    assert trimmed["index_entries"] == [], "summary 已占满预算 → 目录必须裁到 0 条"
    assert trimmed["index_entries_truncated"] is True
    assert _injected_tokens(trimmed, counter) <= BUDGET


def test_trim_prime_truncates_summary_even_without_entries() -> None:
    """目录为空/缺失时也必须扣 summary 的预算（旧实现在这里提前 return）。"""
    counter = _counter()
    long_summary = " ".join(["画像描述"] * 200)
    for prime in ({"summary": long_summary}, {"summary": long_summary, "index_entries": []}):
        trimmed, truncated = trim_prime_to_budget(
            prime, budget_tokens=BUDGET, token_counter=counter
        )
        assert truncated is True, f"{prime.keys()} 的 summary 超预算却没有被裁"
        assert trimmed["summary_truncated"] is True
        assert _injected_tokens(trimmed, counter) <= BUDGET
    # 没有 summary 也没有目录时仍然是 no-op（保持原对象）
    empty: dict[str, Any] = {"index_entries": []}
    assert trim_prime_to_budget(empty, budget_tokens=BUDGET, token_counter=counter) == (
        empty,
        False,
    )


async def test_summary_only_prime_marks_degraded_on_the_node() -> None:
    """节点侧：summary 被裁也要发 memory_prime_degraded，快照 status 转 degraded。"""
    counter = _counter()
    long_summary = " ".join(["画像描述"] * 200)
    prime = _prime(0, summary=long_summary)
    prime["index_entries"] = []
    runtime, writer, _recorder = _node_runtime(prime)
    runtime.token_counter = counter

    result = await recall_memory(_state(), runtime=runtime)

    assert result["memory_prime"]["summary_truncated"] is True
    assert result["memory_context"]["status"] == "degraded"
    assert result["memory_context"]["truncated"] is True
    assert [write.payload["flags"] for write in writer.writes] == [["memory_prime_degraded"]]
    TurnDegradedPayload.model_validate(writer.writes[0].payload)
    assert _injected_tokens(result["memory_prime"], counter) <= BUDGET


def test_trim_prime_ignores_empty_or_invalid_entries() -> None:
    counter = _counter()
    for prime in ({}, {"index_entries": []}, {"index_entries": None}, {"index_entries": "x"}):
        assert trim_prime_to_budget(prime, budget_tokens=BUDGET, token_counter=counter) == (
            prime,
            False,
        )


# ---------------------------------------------------------------------------
# 节点侧：注入有界 + 可观测降级标记 + pin 记录实际条数
# ---------------------------------------------------------------------------


class _FakeSession:
    """`async with factory() as session` + `session.begin()` 的最小替身。"""

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _FakeSession:
        return self


class _FakeRepository:
    """只提供 `_emit_degraded` 需要的 session_factory。"""

    def __init__(self) -> None:
        self.session_factory = _FakeSession


class _RecordingEventWriter:
    def __init__(self) -> None:
        self.writes: list[Any] = []

    async def append(self, session: Any, *, write: Any) -> None:
        self.writes.append(write)


class _FakeRecorder:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    async def record(
        self, record_type: str, *, payload: dict[str, Any], turn_id: Any = None
    ) -> bool:
        self.records.append((record_type, payload))
        return True


class _PrimeGateway:
    def __init__(self, prime: dict[str, Any]) -> None:
        self.prime = prime
        self.calls = 0

    async def build_memory_prime(self, *, user_id: str | None = None) -> dict[str, Any]:
        self.calls += 1
        return dict(self.prime)


def _node_runtime(prime: dict[str, Any]) -> tuple[Any, _RecordingEventWriter, _FakeRecorder]:
    runtime = build_runtime(memory_gateway=_PrimeGateway(prime))
    runtime.flags = {**runtime.flags, "memory_prime": True}
    runtime.settings = Settings(app_env="test", conversation_memory_token_budget=BUDGET)
    runtime.token_counter = _counter()
    writer = _RecordingEventWriter()
    runtime.turn_event_writer = writer
    runtime.conversation_repository = _FakeRepository()
    recorder = _FakeRecorder()
    runtime.rollout_recorder = recorder
    return runtime, writer, recorder


def _state(*, first_turn: bool = True) -> dict[str, Any]:
    recent = [] if first_turn else [{"role": "user", "content": "上一轮问题"}]
    return {
        "user_id": str(uuid4()),
        "thread_id": str(uuid4()),
        "turn_id": str(uuid4()),
        "request_id": "req-1",
        "run_id": "run-1",
        "conversation_context": {"recent_messages": recent},
    }


async def test_prime_injection_is_bounded_and_flagged() -> None:
    """主题很多时：注入有界、快照标记 degraded、发 memory_prime_degraded（不静默丢内容）。"""
    runtime, writer, recorder = _node_runtime(_prime(ENTRY_COUNT))

    result = await recall_memory(_state(), runtime=runtime)

    prime = result["memory_prime"]
    assert 0 < len(prime["index_entries"]) < ENTRY_COUNT
    assert prime["index_entries_truncated"] is True
    assert _injected_tokens(prime, runtime.token_counter) <= BUDGET
    # 注进快照/answer 视图的那份与 memory_prime 是同一份（同一个裁剪结果）
    assert result["memory_context"]["prime"]["index_entries"] == prime["index_entries"]
    assert result["memory_context"]["truncated"] is True
    assert result["memory_context"]["status"] == "degraded"
    # 可观测：走既有 turn.degraded 机制，flag 必须能过封闭 DegradedFlag 校验
    assert [write.payload["flags"] for write in writer.writes] == [["memory_prime_degraded"]]
    TurnDegradedPayload.model_validate(writer.writes[0].payload)
    # pin 记录的是实际注入的条数与截断事实（审计能看出这个 thread 的提示词被裁过）
    assert [record_type for record_type, _ in recorder.records] == ["memory_prime"]
    pinned = recorder.records[0][1]
    assert pinned["index_entry_count"] == len(prime["index_entries"])
    assert pinned["truncated"] is True


async def test_prime_injection_under_budget_is_not_degraded() -> None:
    """预算内的正常路径不得产生假降级：条数不变、无标记、status 保持 available。"""
    runtime, writer, recorder = _node_runtime(_prime(2))

    result = await recall_memory(_state(), runtime=runtime)

    prime = result["memory_prime"]
    assert len(prime["index_entries"]) == 2
    assert prime["index_entries_truncated"] is False
    assert result["memory_context"]["truncated"] is False
    assert result["memory_context"]["status"] == "available"
    assert writer.writes == []
    assert recorder.records[0][1]["index_entry_count"] == 2
    assert recorder.records[0][1]["truncated"] is False


async def test_prime_service_side_truncation_also_degrades() -> None:
    """服务端已按条数上限截断（index_entries_truncated=true）时，节点侧照发降级标记。"""
    prime = _prime(2)
    prime["index_entries_truncated"] = True
    prime["degraded"] = True
    runtime, writer, _recorder = _node_runtime(prime)

    result = await recall_memory(_state(), runtime=runtime)

    assert result["memory_context"]["status"] == "degraded"
    assert result["memory_context"]["truncated"] is True
    assert [write.payload["flags"] for write in writer.writes] == [["memory_prime_degraded"]]


# ---------------------------------------------------------------------------
# 快照与 answer 视图：有界不变量在出口也成立
# ---------------------------------------------------------------------------


def _context_service(budget: int = BUDGET) -> ContextService:
    return ContextService(
        settings=Settings(app_env="test", conversation_memory_token_budget=budget),
        token_counter=_counter(),
    )


def test_snapshot_freezes_bounded_prime_invariant() -> None:
    service = _context_service()
    snapshot = service.build_snapshot(
        user_id=uuid4(),
        thread_id=uuid4(),
        turn_id=uuid4(),
        current_message="椭圆",
        recent_messages=[],
        conversation_summary=None,
        memory={"status": "available", "prime": _prime(ENTRY_COUNT), "truncated": False},
        memory_status="available",
    )

    prime = snapshot.memory.prime
    assert 0 < len(prime["index_entries"]) < ENTRY_COUNT
    assert prime["index_entries_truncated"] is True
    # 快照的 truncated 一并置位，SSE 的"已读取部分相关记忆"与实际一致
    assert snapshot.memory.truncated is True
    assert _injected_tokens(prime, _counter()) <= BUDGET


def test_answer_view_without_prime_keeps_flag_off_shape() -> None:
    """flag 关闭（快照没有 prime）时：视图里不出现 prime 键，裁剪是零副作用。

    review-2 新发现 10 的修复让 summary 也参与预算，这条断言固定"关闭路径逐字不变"。
    """
    service = _context_service()
    snapshot = TurnContextSnapshot(
        snapshot_id="s-1",
        current_message="椭圆",
        memory=SnapshotMemory(status="available"),
    )

    view = service.build_answer_view(
        snapshot=snapshot,
        standalone_question="椭圆是什么",
        evidence_summary="",
        evidence_refs=[],
        degraded_flags=[],
    )

    assert "prime" not in view["long_term_memory"]
    empty: dict[str, Any] = {}
    trimmed, truncated = trim_prime_to_budget(empty, budget_tokens=BUDGET, token_counter=_counter())
    assert trimmed is empty and truncated is False


def test_answer_view_trims_unbounded_prime_from_old_checkpoint() -> None:
    """兜底：旧 checkpoint 里的快照可能带未裁剪的 prime，视图出口仍必须有界。"""
    service = _context_service()
    snapshot = TurnContextSnapshot(
        snapshot_id="s-1",
        current_message="椭圆",
        memory=SnapshotMemory(status="available", prime=_prime(ENTRY_COUNT)),
    )

    view = service.build_answer_view(
        snapshot=snapshot,
        standalone_question="椭圆是什么",
        evidence_summary="",
        evidence_refs=[],
        degraded_flags=[],
    )

    prime = view["long_term_memory"]["prime"]
    assert 0 < len(prime["index_entries"]) < ENTRY_COUNT
    assert prime["index_entries_truncated"] is True
    assert _injected_tokens(prime, _counter()) <= BUDGET
