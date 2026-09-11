"""记忆工具循环的节点与图级测试（memory-rebuild §5.7 Phase 5）。

三条验收里"关闭 flag 行为不变"与"tool-call loop 可重复续写"必须同时成立，因此这里
既测节点级的续写拼接，也测图级的路由与 flag 门控：

- 工具轮的 preamble 正文既流出、又拼进最终正文（**已发出的 delta ⊆ 最终正文**）；
- 流式工具轮没有正文是正常形态，不得触发非流式回退；
- `tools is None` 时调用参数与既有实现完全相同（既有 Fake 网关不被破坏）；
- flag 关闭时 `generate_answer` 不会下发工具、路由直接进入引用校验。
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.conversation.graph.builder import _route_after_answer, build_conversation_graph
from backend.conversation.graph.nodes.answer import generate_answer
from tests.conversation.graph_fixtures import (
    FakeOpenAIGateway,
    build_runtime,
    default_rewrite_plan,
)

SEARCH_CALL = {
    "call_id": "call-1",
    "name": "memory.search",
    "arguments": {"queries": ["椭圆"]},
}


class ToolLoopOpenAIGateway(FakeOpenAIGateway):
    """支持记忆工具轮的 Fake 网关（脚本化：先请求工具，再给出最终回答）。"""

    def __init__(self, *, preamble: str = "", answer: str = "椭圆是圆锥曲线之一。") -> None:
        super().__init__()
        self._preamble = preamble
        self._answer = answer
        self.tool_rounds: list[dict[str, Any]] = []
        self.legacy_calls = 0

    def memory_tool_specs(self) -> list[dict[str, Any]]:
        return [{"type": "function", "name": "memory_search"}]

    async def stream_answer(self, **kwargs: Any) -> Any:
        if "tools" not in kwargs:
            self.legacy_calls += 1
            return await super().stream_answer(answer_context=kwargs["answer_context"])
        self.tool_rounds.append(dict(kwargs))
        if len(self.tool_rounds) == 1:
            deltas = list(self._preamble)
            return _ToolTurn(
                deltas=deltas,
                payload={"answer": self._preamble, "followups": [], "citations": []},
                pending=[dict(SEARCH_CALL)],
                response_id="resp-1",
            )
        return _ToolTurn(
            deltas=list(self._answer),
            payload={"answer": self._answer, "followups": ["还要例子吗？"], "citations": []},
            pending=[],
            response_id="resp-2",
        )


class _ToolTurn:
    """`AnswerToolTurn` 的最小等价物（避免节点测试依赖具体实现类）。"""

    def __init__(
        self,
        *,
        deltas: list[str],
        payload: dict[str, Any],
        pending: list[dict[str, Any]],
        response_id: str | None,
    ) -> None:
        self.deltas = deltas
        self.payload = payload
        self.pending_tool_calls = pending
        self.response_id = response_id


class ToolLoopMemoryGateway:
    """只实现两个工具方法的 Fake 记忆网关。"""

    def __init__(self) -> None:
        self.searches: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []

    async def search_memories(
        self,
        *,
        queries: list[str],
        match_mode: str = "any",
        max_results: int = 10,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.searches.append({"queries": queries, "match_mode": match_mode})
        return {
            "items": [
                {
                    "memory_id": "mastery:椭圆",
                    "name": "椭圆",
                    "description": "圆锥曲线之一",
                    "keywords": [],
                    "version": 3,
                    "updated_at": "2026-09-10T05:00:00Z",
                }
            ],
            "truncated": False,
        }

    async def read_memory(self, **kwargs: Any) -> dict[str, Any]:
        self.reads.append(kwargs)
        return {
            "memory_id": kwargs.get("memory_id"),
            "version": 3,
            "checksum": "b" * 64,
            "content": "椭圆正文",
            "line_offset": 0,
            "total_lines": 1,
            "truncated": False,
        }


class StreamingToolLoopGateway(ToolLoopOpenAIGateway):
    """带真实流式能力的工具轮网关：工具轮零正文、第二轮才有正文。"""

    def supports_answer_streaming(self) -> bool:
        return True

    def open_answer_stream(self, **kwargs: Any) -> Any:
        self.tool_rounds.append(dict(kwargs))
        first = len(self.tool_rounds) == 1
        return _ToolStream(pending=[dict(SEARCH_CALL)] if first else [])


class _ToolStream:
    def __init__(self, *, pending: list[dict[str, Any]]) -> None:
        self._pending = pending
        self._chunks = [] if pending else ["椭圆", "是圆锥曲线。"]
        self.followups: list[str] = []
        self.truncated = False
        self.refused = False
        self.response_id = "resp-stream-1"

    @property
    def pending_tool_calls(self) -> list[dict[str, Any]]:
        return [dict(call) for call in self._pending]

    async def __aiter__(self) -> Any:
        for chunk in self._chunks:
            yield chunk


def _state(**overrides: Any) -> dict[str, Any]:
    from uuid import uuid4

    state: dict[str, Any] = {
        "user_id": uuid4(),
        "thread_id": uuid4(),
        "turn_id": uuid4(),
        "request_id": "req-1",
        "run_id": "run-1",
        "user_message_id": uuid4(),
        # 与 tests/conversation/test_streaming_answer_node.py 相同的快照形状：
        # answer 节点会把它反序列化成 TurnContextSnapshot。
        "snapshot": {
            "snapshot_id": "test-snapshot",
            "current_message": "椭圆是什么？",
            "recent_messages": [],
            "conversation_summary": None,
        },
        "evidence_set": {"items": []},
        "degraded_flags": [],
        "rewrite_plan": {"standalone_question": "椭圆是什么？"},
        "_memory_tools_enabled": True,
    }
    state.update(overrides)
    return state


def _runtime(**overrides: Any) -> Any:
    runtime = build_runtime(
        openai_gateway=overrides.pop("openai_gateway", ToolLoopOpenAIGateway()),
        memory_gateway=overrides.pop("memory_gateway", ToolLoopMemoryGateway()),
    )
    for key, value in overrides.items():
        setattr(runtime, key, value)
    return runtime


# ---------------------------------------------------------------------------
# 节点级：两轮续写
# ---------------------------------------------------------------------------


def _capture_deltas(monkeypatch: Any) -> list[str]:
    """把 answer.delta 的写出重定向到一个列表（runtime 无 DB session，默认会跳过）。"""
    from backend.conversation.graph.nodes import answer as answer_module

    recorded: list[str] = []

    async def fake_write(
        runtime: Any,
        repo: Any,
        turn_id: Any,
        request_id: str,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if event_type == "answer.delta":
            recorded.append(str(payload.get("text_delta") or ""))

    monkeypatch.setattr(answer_module, "_write_event", fake_write)
    return recorded


@pytest.mark.asyncio
async def test_second_round_is_continued_and_texts_are_concatenated(monkeypatch: Any) -> None:
    openai = ToolLoopOpenAIGateway(preamble="让我查一下。")
    runtime = _runtime(openai_gateway=openai)
    recorded = _capture_deltas(monkeypatch)

    first = await generate_answer(
        _state(), runtime=runtime, context_service=runtime.context_service
    )
    assert first["memory_pending_tool_calls"] == [SEARCH_CALL]
    assert first["memory_response_id"] == "resp-1"
    assert first["answer_buffer"] == "让我查一下。"

    second = await generate_answer(
        _state(
            memory_tool_outputs=[{"call_id": "call-1", "name": "memory.search", "output": "{}"}],
            memory_response_id=first["memory_response_id"],
            answer_buffer=first["answer_buffer"],
        ),
        runtime=runtime,
        context_service=runtime.context_service,
    )

    # 第二轮带上了续写锚点与工具结果
    assert openai.tool_rounds[1]["previous_response_id"] == "resp-1"
    assert openai.tool_rounds[1]["tool_outputs"]
    # 最终正文 = 工具轮 preamble + 续写正文
    assert second["answer_payload"]["answer"] == "让我查一下。椭圆是圆锥曲线之一。"
    assert second["answer_buffer"] == second["answer_payload"]["answer"]
    assert second["memory_pending_tool_calls"] == []
    # 已发出的 delta 拼接后等于最终正文（不变量）
    assert "".join(recorded) == second["answer_payload"]["answer"]
    # followups 只取最后一段
    assert second["answer_payload"]["followups"] == ["还要例子吗？"]


@pytest.mark.asyncio
async def test_tools_are_not_passed_when_flag_is_off() -> None:
    openai = ToolLoopOpenAIGateway()
    openai.answer_payloads.append({"answer": "普通回答", "citations": [], "followups": []})
    runtime = _runtime(openai_gateway=openai)

    result = await generate_answer(
        _state(_memory_tools_enabled=False),
        runtime=runtime,
        context_service=runtime.context_service,
    )

    assert openai.legacy_calls == 1
    assert openai.tool_rounds == []
    assert result["answer_payload"]["answer"] == "普通回答"
    assert "memory_pending_tool_calls" not in result


@pytest.mark.asyncio
async def test_streaming_tool_round_without_text_does_not_fall_back(monkeypatch: Any) -> None:
    openai = StreamingToolLoopGateway()
    runtime = _runtime(openai_gateway=openai)
    recorded = _capture_deltas(monkeypatch)

    result = await generate_answer(
        _state(), runtime=runtime, context_service=runtime.context_service
    )

    # 工具轮零正文是正常形态：不回退、不报错，且把调用交给上层
    assert result["memory_pending_tool_calls"] == [SEARCH_CALL]
    assert result["memory_response_id"] == "resp-stream-1"
    assert result["answer_buffer"] == ""
    assert recorded == []


@pytest.mark.asyncio
async def test_streaming_continuation_appends_to_carried_text(monkeypatch: Any) -> None:
    openai = StreamingToolLoopGateway()
    runtime = _runtime(openai_gateway=openai)
    captured = _capture_deltas(monkeypatch)
    # 先制造一次工具轮，让网关的内部轮次推进到第二轮
    await generate_answer(_state(), runtime=runtime, context_service=runtime.context_service)

    result = await generate_answer(
        _state(
            memory_tool_outputs=[{"call_id": "call-1", "name": "memory.search", "output": "{}"}],
            memory_response_id="resp-stream-1",
            answer_buffer="让我查一下。",
        ),
        runtime=runtime,
        context_service=runtime.context_service,
    )

    assert result["answer_payload"]["answer"] == "让我查一下。椭圆是圆锥曲线。"
    # 第二轮只流出**新增**正文；preamble 在上一轮已经流出过，不重复发
    assert "".join(captured) == "椭圆是圆锥曲线。"


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def test_route_after_answer_gates_on_flag_and_budget() -> None:
    pending = {"memory_pending_tool_calls": [SEARCH_CALL]}
    assert _route_after_answer({**pending, "_memory_tools_enabled": True}) == "memory_tool"
    assert _route_after_answer({**pending, "_memory_tools_enabled": False}) == "validate"
    assert _route_after_answer({"_memory_tools_enabled": True}) == "validate"
    assert (
        _route_after_answer({**pending, "_memory_tools_enabled": True, "memory_tool_calls": 6})
        == "validate"
    )


# ---------------------------------------------------------------------------
# 图级：完整 tool-call loop
# ---------------------------------------------------------------------------


async def _run_graph(runtime: Any, state: dict[str, Any]) -> dict[str, Any]:
    from backend.conversation.contracts.retrieval import ActiveCorpusVocabulary

    graph = build_conversation_graph(runtime_context=runtime, vocabulary=ActiveCorpusVocabulary())
    compiled = graph.compile()
    return await compiled.ainvoke(state)


@pytest.mark.asyncio
async def test_graph_runs_full_tool_loop_once() -> None:
    from tests.conversation.test_conversation_graph import _initial_state

    openai = ToolLoopOpenAIGateway(preamble="让我查一下。")
    memory = ToolLoopMemoryGateway()
    runtime = _runtime(openai_gateway=openai, memory_gateway=memory)
    runtime.flags = {**runtime.flags, "memory_tools": True, "memory_read": True}
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=0, need_retrieval=False))

    state = _initial_state()
    state["conversation_context"] = {"recent_messages": [{"role": "user", "content": "椭圆"}]}
    result = await _run_graph(runtime, state)

    # 图里真的执行了一次 search，并把两轮正文拼成最终回答
    assert [call["queries"] for call in memory.searches] == [["椭圆"]]
    assert result["answer_payload"]["answer"] == "让我查一下。椭圆是圆锥曲线之一。"
    assert result["memory_tool_calls"] == 1
    assert result["memory_tool_rounds"] == 1
    assert result["memory_pending_tool_calls"] == []


@pytest.mark.asyncio
async def test_graph_with_flag_off_keeps_legacy_path() -> None:
    from tests.conversation.test_conversation_graph import _initial_state

    openai = ToolLoopOpenAIGateway()
    openai.answer_payloads.append({"answer": "旧路径回答", "citations": [], "followups": []})
    memory = ToolLoopMemoryGateway()
    runtime = _runtime(openai_gateway=openai, memory_gateway=memory)
    runtime.flags = {**runtime.flags, "memory_tools": False, "memory_read": False}
    openai.rewrite_queue.append(default_rewrite_plan(subqueries=0, need_retrieval=False))

    result = await _run_graph(runtime, _initial_state())

    assert openai.tool_rounds == []
    assert openai.legacy_calls == 1
    assert memory.searches == []
    assert result["answer_payload"]["answer"] == "旧路径回答"


# ---------------------------------------------------------------------------
# finalize：记忆引用进 answer.completed 的结构化字段（§5.7）
# ---------------------------------------------------------------------------


def test_answer_completed_payload_carries_memory_citations() -> None:
    from backend.conversation.contracts.events import validate_event_payload
    from backend.conversation.graph.nodes.finalize import build_answer_completed_payload

    citation = {
        "memory_id": "mastery:椭圆",
        "name": "椭圆",
        "version": 3,
        "checksum": "c" * 64,
        "line_offset": 0,
        "total_lines": 12,
        "truncated": False,
    }
    payload = build_answer_completed_payload(
        assistant_message_id="00000000-0000-0000-0000-000000000001",
        thread_version=2,
        answer="椭圆是圆锥曲线之一。",
        citations=[],
        followups=[],
        degraded_flags=["memory_tool_degraded"],
        memory_citations=[citation],
    )

    # 真正落库前会跑一次 extra="forbid" 的契约校验
    validated = validate_event_payload("answer.completed", payload)
    assert validated["memory_citations"] == [citation]
    assert validated["degraded_flags"] == ["memory_tool_degraded"]


def test_answer_completed_payload_defaults_to_empty_memory_citations() -> None:
    from backend.conversation.contracts.events import validate_event_payload
    from backend.conversation.graph.nodes.finalize import build_answer_completed_payload

    payload = build_answer_completed_payload(
        assistant_message_id="00000000-0000-0000-0000-000000000002",
        thread_version=1,
        answer="普通回答",
        citations=[],
        followups=[],
        degraded_flags=[],
    )
    # 关闭记忆工具时形状与既有实现一致（空列表，不新增必需字段）
    assert validate_event_payload("answer.completed", payload)["memory_citations"] == []


# ---------------------------------------------------------------------------
# prime → 提示词（§2.4 D1）：必须真的进 answer 视图，且 flag 关闭时不进
# ---------------------------------------------------------------------------


def _snapshot(runtime: Any, memory: dict[str, Any] | None) -> Any:
    from uuid import uuid4

    return runtime.context_service.build_snapshot(
        user_id=uuid4(),
        thread_id=uuid4(),
        turn_id=uuid4(),
        current_message="椭圆是什么？",
        recent_messages=[],
        conversation_summary=None,
        memory=memory,
        memory_status=str((memory or {}).get("status") or "unavailable"),
    )


def _view(runtime: Any, snapshot: Any) -> dict[str, Any]:
    return runtime.context_service.build_answer_view(
        snapshot=snapshot,
        standalone_question="椭圆是什么？",
        evidence_summary="",
        evidence_refs=[],
        degraded_flags=[],
    )


def test_prime_reaches_answer_view_and_survives_checkpoint_round_trip() -> None:
    from backend.conversation.graph.state import serialize_snapshot, snapshot_from_dict

    runtime = build_runtime()
    prime = {
        "summary": "用户正在学圆锥曲线。",
        "schema_version": "v1",
        "index_entries": [{"memory_id": "mastery:椭圆", "name": "椭圆"}],
        "degraded": False,
    }
    snapshot = _snapshot(runtime, {"status": "available", "prime": prime})

    view = _view(runtime, snapshot)
    assert view["long_term_memory"]["prime"]["summary"] == "用户正在学圆锥曲线。"
    assert view["long_term_memory"]["prime"]["index_entries"][0]["memory_id"] == "mastery:椭圆"

    # 进 checkpoint 再取回：prime 必须还在（否则 resume 后提示词会静默变化）
    restored = snapshot_from_dict(serialize_snapshot(snapshot))
    assert restored.memory.prime["summary"] == "用户正在学圆锥曲线。"


def test_answer_view_has_no_prime_key_when_prime_mode_is_off() -> None:
    runtime = build_runtime()
    snapshot = _snapshot(runtime, {"status": "available", "learner": {"goal": "高考"}})

    view = _view(runtime, snapshot)
    # 关闭 prime 时视图形状与既有实现逐字一致（不多一个 prime 键）
    assert "prime" not in view["long_term_memory"]
    assert view["long_term_memory"]["status"] == "available"
