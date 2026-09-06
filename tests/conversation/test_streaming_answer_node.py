"""generate_answer 真实流式路径的节点测试（§15.4 流式模式）。

覆盖：delta 转发与 followups、中断保留部分正文、首 token 前失败回退非流式、
空正文（自然结束/拒绝）回退、incomplete 截断标记、真实 AnswerTextStream 协议集成。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

from backend.conversation.contracts.errors import ModelUnavailableError
from backend.conversation.gateways.openai import AnswerTextStream
from backend.conversation.graph.nodes.answer import generate_answer
from tests.conversation.graph_fixtures import build_runtime


class _FakeAnswerStream:
    """模拟 AnswerTextStream 接口（full_text/followups + 异步迭代）。

    ``fail_after`` 表示第 N 个 chunk 发出之前抛 ModelUnavailableError：
    0 表示首 token 前失败（触发非流式回退）；正数表示发出 N 个后中断。
    """

    def __init__(
        self,
        chunks: list[str],
        followups: list[str] | None = None,
        fail_after: int | None = None,
        truncated: bool = False,
        refused: bool = False,
    ) -> None:
        self._chunks = list(chunks)
        self._followups = list(followups or [])
        self._fail_after = fail_after
        self._truncated = truncated
        self._refused = refused
        self._yielded: list[str] = []

    @property
    def followups(self) -> list[str]:
        return list(self._followups)

    @property
    def full_text(self) -> str:
        return "".join(self._yielded)

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def refused(self) -> bool:
        return self._refused

    async def __aiter__(self) -> Any:
        for chunk in self._chunks:
            if self._fail_after is not None and len(self._yielded) >= self._fail_after:
                raise ModelUnavailableError("模拟流式中断")
            self._yielded.append(chunk)
            yield chunk
        if self._fail_after is not None and len(self._yielded) >= self._fail_after:
            raise ModelUnavailableError("模拟流式中断")


class FakeStreamingGateway:
    """带真实流式能力的 Fake 网关（含回退记录）。"""

    def __init__(
        self,
        chunks: list[str],
        followups: list[str] | None = None,
        fail_after: int | None = None,
        truncated: bool = False,
        refused: bool = False,
    ) -> None:
        self._chunks = chunks
        self._followups = followups
        self._fail_after = fail_after
        self._truncated = truncated
        self._refused = refused
        self.fallback_calls = 0

    def supports_answer_streaming(self) -> bool:
        return True

    def open_answer_stream(self, *, answer_context: dict[str, Any]) -> _FakeAnswerStream:
        return _FakeAnswerStream(
            self._chunks,
            followups=self._followups,
            fail_after=self._fail_after,
            truncated=self._truncated,
            refused=self._refused,
        )

    async def stream_answer(
        self, *, answer_context: dict[str, Any]
    ) -> tuple[list[str], dict[str, Any]]:
        self.fallback_calls += 1
        return ["回退正文"], {"answer": "回退正文", "followups": [], "citations": []}


class RealProtocolGateway:
    """节点 + 真实 AnswerTextStream 标记协议集成（Critical #1 回归）。"""

    def __init__(self, events: list[Any]) -> None:
        self._events = events
        self.fallback_calls = 0

    def supports_answer_streaming(self) -> bool:
        return True

    def open_answer_stream(self, *, answer_context: dict[str, Any]) -> AnswerTextStream:
        return AnswerTextStream(
            client=_FakeClientWithStream(self._events),
            model="answer-model",
            user_payload="{}",
            logger=logging.getLogger("conversation.test.streaming"),
            reasoning_effort="low",
        )

    async def stream_answer(
        self, *, answer_context: dict[str, Any]
    ) -> tuple[list[str], dict[str, Any]]:
        self.fallback_calls += 1
        return [], {}


class _FakeAsyncStream:
    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)

    def __aiter__(self) -> _FakeAsyncStream:
        self._it = iter(self._events)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def close(self) -> None:
        pass


class _FakeClientWithStream:
    def __init__(self, events: list[Any]) -> None:
        self.responses = _FakeResponses(events)


class _FakeResponses:
    def __init__(self, events: list[Any]) -> None:
        self._events = events

    async def create(self, **kwargs: Any) -> Any:
        return _FakeAsyncStream(self._events)


def _state() -> dict[str, Any]:
    return {
        "turn_id": "turn-1",
        "request_id": "req-1",
        "run_id": "run-1",
        "snapshot": {
            "snapshot_id": "test-snapshot",
            "current_message": "勾股定理是什么？",
            "recent_messages": [],
            "conversation_summary": None,
        },
        "rewrite_plan": {
            "standalone_question": "勾股定理是什么？",
            "need_retrieval": True,
            "retrieval_decision": {
                "decision": "retrieve",
                "basis_codes": ["TEXTBOOK_FACT_REQUIRED"],
                "rationale": "需要教材事实。",
            },
        },
        "evidence_set": {"items": []},
        "degraded_flags": [],
    }


async def test_streaming_answer_delivers_body_without_followups_marker() -> None:
    gateway = FakeStreamingGateway(["勾股定理：", "a²+b²=c²"], followups=["再多讲讲"])
    runtime = build_runtime(openai_gateway=gateway)
    result = await generate_answer(
        _state(), runtime=runtime, context_service=runtime.context_service
    )

    assert result["answer_payload"]["answer"] == "勾股定理：a²+b²=c²"
    assert result["answer_payload"]["followups"] == ["再多讲讲"]
    assert gateway.fallback_calls == 0


async def test_streaming_interrupt_keeps_partial_text_and_marks_degraded() -> None:
    gateway = FakeStreamingGateway(["前半句", "后半句"], fail_after=1)
    runtime = build_runtime(openai_gateway=gateway)
    result = await generate_answer(
        _state(), runtime=runtime, context_service=runtime.context_service
    )

    assert result["answer_payload"]["answer"] == "前半句"
    assert result["answer_payload"]["followups"] == []
    assert "answer_stream_interrupted" in result.get("degraded_flags", [])
    assert gateway.fallback_calls == 0


async def test_streaming_failure_before_first_token_falls_back_to_structured() -> None:
    gateway = FakeStreamingGateway([], fail_after=0)
    runtime = build_runtime(openai_gateway=gateway)
    result = await generate_answer(
        _state(), runtime=runtime, context_service=runtime.context_service
    )

    assert result["answer_payload"]["answer"] == "回退正文"
    assert gateway.fallback_calls == 1


async def test_streaming_empty_natural_end_falls_back_to_structured() -> None:
    """自然结束零正文（refusal/空输出）必须回退，不能静默成功（评审 #2）。"""
    gateway = FakeStreamingGateway([])
    runtime = build_runtime(openai_gateway=gateway)
    result = await generate_answer(
        _state(), runtime=runtime, context_service=runtime.context_service
    )

    assert result["answer_payload"]["answer"] == "回退正文"
    assert gateway.fallback_calls == 1


async def test_streaming_truncated_marks_degraded() -> None:
    """incomplete 截断（含部分正文）→ 保留正文 + answer_stream_truncated。"""
    gateway = FakeStreamingGateway(["前半", "后半"], truncated=True)
    runtime = build_runtime(openai_gateway=gateway)
    result = await generate_answer(
        _state(), runtime=runtime, context_service=runtime.context_service
    )

    assert result["answer_payload"]["answer"] == "前半后半"
    assert "answer_stream_truncated" in result.get("degraded_flags", [])
    assert gateway.fallback_calls == 0


async def test_streaming_real_protocol_marker_isolated_delta() -> None:
    """Critical #1 node 级回归：<followups> 独占 delta 时不得泄漏进正文。"""
    gateway = RealProtocolGateway(
        [
            SimpleNamespace(type="response.output_text.delta", delta="正文"),
            SimpleNamespace(type="response.output_text.delta", delta="<followups>"),
            SimpleNamespace(type="response.output_text.delta", delta='["追问A"]'),
            SimpleNamespace(type="response.output_text.delta", delta="</followups>"),
        ]
    )
    runtime = build_runtime(openai_gateway=gateway)
    result = await generate_answer(
        _state(), runtime=runtime, context_service=runtime.context_service
    )

    assert result["answer_payload"]["answer"] == "正文"
    assert result["answer_payload"]["followups"] == ["追问A"]
    assert gateway.fallback_calls == 0
