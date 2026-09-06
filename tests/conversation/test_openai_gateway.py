"""Conversation OpenAI Gateway 单元测试。

覆盖 Responses 状态识别、兼容端点正文回退、有限重试，以及 Answer 完整校验后
应用层切分正文，避免依赖真实模型和网络。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from backend.conversation.contracts.errors import ModelUnavailableError, StructuredOutputError
from backend.conversation.contracts.graph import AnswerGenerationOutput
from backend.conversation.gateways import openai as gateway_module
from backend.conversation.gateways.openai import (
    ANSWER_DELTA_CHARS,
    OpenAIGateway,
    _parse_structured_response,
)


class TinyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)


def _response(
    *,
    status: str = "completed",
    output_text: str | None = None,
    output: Any = None,
    reason: str | None = None,
) -> Any:
    return SimpleNamespace(
        status=status,
        output_text=output_text,
        output=output or [],
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
    )


def test_incomplete_response_is_classified_with_reason() -> None:
    with pytest.raises(StructuredOutputError) as caught:
        _parse_structured_response(
            _response(status="incomplete", reason="max_output_tokens"),
            text_format=TinyOutput,
            attempt=1,
        )

    assert caught.value.reason == "incomplete"
    assert caught.value.response_status == "incomplete"
    assert caught.value.incomplete_reason == "max_output_tokens"


def test_output_text_falls_back_to_response_output_content() -> None:
    response = _response(
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text='{"answer":"回退正文"}')],
            )
        ]
    )

    parsed = _parse_structured_response(response, text_format=TinyOutput, attempt=1)

    assert parsed.answer == "回退正文"


def test_empty_output_is_classified() -> None:
    with pytest.raises(StructuredOutputError) as caught:
        _parse_structured_response(_response(), text_format=TinyOutput, attempt=1)

    assert caught.value.reason == "empty_output"


@pytest.mark.asyncio
async def test_structured_retries_empty_then_succeeds() -> None:
    responses = [_response(), _response(output_text='{"answer":"重试成功"}')]
    calls: list[dict[str, Any]] = []

    class FakeResponses:
        async def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return responses.pop(0)

    class FakeClient:
        responses = FakeResponses()

    gateway = object.__new__(OpenAIGateway)
    gateway._client = FakeClient()
    gateway._settings = SimpleNamespace(
        openai_reasoning_effort="max", openai_answer_model="answer-model"
    )
    gateway._logger = gateway_module.logging.getLogger("test.openai.gateway")

    parsed = await gateway._structured(
        role="answer",
        system_prompt="system",
        user_payload="{}",
        text_format=TinyOutput,
    )

    assert parsed.answer == "重试成功"
    assert len(calls) == 2
    assert calls[0]["reasoning"]["effort"] == "max"
    assert calls[0]["timeout"] == 120.0
    assert "结构化输出重试" in calls[1]["input"][0]["content"]


@pytest.mark.asyncio
async def test_structured_failure_retries_three_times_then_raises() -> None:
    calls = 0

    class FakeResponses:
        async def create(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return _response(status="incomplete", reason="max_output_tokens")

    class FakeClient:
        responses = FakeResponses()

    gateway = object.__new__(OpenAIGateway)
    gateway._client = FakeClient()
    gateway._settings = SimpleNamespace(
        openai_reasoning_effort="none", openai_answer_model="answer-model"
    )
    gateway._logger = gateway_module.logging.getLogger("test.openai.gateway")

    with pytest.raises(StructuredOutputError) as caught:
        await gateway._structured(
            role="answer",
            system_prompt="system",
            user_payload="{}",
            text_format=TinyOutput,
        )

    assert calls == 3
    assert caught.value.attempts == 3
    assert caught.value.incomplete_reason == "max_output_tokens"


@pytest.mark.asyncio
async def test_stream_answer_uses_non_streaming_structured_output_and_body_deltas() -> None:
    answer = "甲" * (ANSWER_DELTA_CHARS + 3)
    calls: list[dict[str, Any]] = []

    class FakeResponses:
        async def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return _response(output_text='{"answer":"' + answer + '","followups":[]}')

    class FakeClient:
        responses = FakeResponses()

    gateway = object.__new__(OpenAIGateway)
    gateway._client = FakeClient()
    gateway._settings = SimpleNamespace(
        openai_reasoning_effort="none", openai_answer_model="answer-model"
    )
    gateway._logger = gateway_module.logging.getLogger("test.openai.gateway")

    deltas, payload = await gateway.stream_answer(answer_context={"question": "q"})

    assert calls and calls[0].get("stream") is None
    assert payload == {"answer": answer, "followups": []}
    assert "".join(deltas) == answer
    assert all(len(delta) <= ANSWER_DELTA_CHARS for delta in deltas)
    assert all(not delta.startswith("{") for delta in deltas)
    assert isinstance(payload, dict)


@pytest.mark.parametrize(
    "output_model",
    [AnswerGenerationOutput],
)
def test_answer_generation_schema_excludes_citations(output_model: type[BaseModel]) -> None:
    fields = output_model.model_fields
    assert set(fields) == {"answer", "followups"}
    assert "citations" not in fields


def test_model_unavailable_remains_retryable() -> None:
    error = ModelUnavailableError("不可用")
    assert error.code == "MODEL_UNAVAILABLE"
    assert error.retryable is True


def test_refusal_is_classified_without_parsing_body() -> None:
    response = _response(
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="refusal", refusal="无法回答")],
            )
        ]
    )

    with pytest.raises(StructuredOutputError) as caught:
        _parse_structured_response(response, text_format=TinyOutput, attempt=1)

    assert caught.value.reason == "refusal"


@pytest.mark.asyncio
async def test_truncated_json_retries_then_reports_schema_invalid() -> None:
    calls = 0

    class FakeResponses:
        async def create(self, **kwargs: Any) -> Any:
            nonlocal calls
            calls += 1
            return _response(output_text='{"answer":')

    class FakeClient:
        responses = FakeResponses()

    gateway = object.__new__(OpenAIGateway)
    gateway._client = FakeClient()
    gateway._settings = SimpleNamespace(
        openai_reasoning_effort="none", openai_answer_model="answer-model"
    )
    gateway._logger = gateway_module.logging.getLogger("test.openai.gateway")

    with pytest.raises(StructuredOutputError) as caught:
        await gateway._structured(
            role="answer",
            system_prompt="system",
            user_payload="{}",
            text_format=TinyOutput,
        )

    assert calls == 3
    assert caught.value.reason == "schema_invalid"
    assert caught.value.attempts == 3


@pytest.mark.asyncio
async def test_rewrite_and_evidence_force_reasoning_none() -> None:
    calls: list[dict[str, Any]] = []

    class FakeResponses:
        async def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return _response(output_text='{"answer":"ok"}')

    class FakeClient:
        responses = FakeResponses()

    gateway = object.__new__(OpenAIGateway)
    gateway._client = FakeClient()
    gateway._settings = SimpleNamespace(
        openai_reasoning_effort="max",
        openai_rewrite_model="rewrite-model",
        openai_evidence_model="evidence-model",
    )
    gateway._logger = gateway_module.logging.getLogger("test.openai.gateway")

    await gateway._structured(
        role="rewrite",
        system_prompt="system",
        user_payload="{}",
        text_format=TinyOutput,
    )
    await gateway._structured(
        role="evidence",
        system_prompt="system",
        user_payload="{}",
        text_format=TinyOutput,
    )

    assert [call["reasoning"]["effort"] for call in calls] == ["none", "none"]
    assert [call["timeout"] for call in calls] == [30.0, 30.0]


class _FakeAsyncStream:
    """模拟 SDK 流式响应：事件迭代 + close。"""

    def __init__(self, events: list[Any]) -> None:
        self._events = list(events)
        self.closed = False

    def __aiter__(self) -> _FakeAsyncStream:
        self._it = iter(self._events)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._it)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def close(self) -> None:
        self.closed = True


def _delta(text: str) -> Any:
    """模拟 SDK 的 response.output_text.delta 事件。"""
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def _stream_gateway(events: list[Any]) -> tuple[OpenAIGateway, _FakeAsyncStream]:
    class FakeResponses:
        async def create(self, **kwargs: Any) -> Any:
            assert kwargs.get("stream") is True
            return stream

    class FakeClient:
        responses = FakeResponses()

    stream = _FakeAsyncStream(events)
    gateway = object.__new__(OpenAIGateway)
    gateway._client = FakeClient()
    gateway._settings = SimpleNamespace(conversation_answer_streaming=True)
    gateway._logger = gateway_module.logging.getLogger("test.openai.gateway")
    return gateway, stream


def _text_stream(gateway: OpenAIGateway, user_payload: str = "{}") -> Any:
    from backend.conversation.gateways.openai import AnswerTextStream

    return AnswerTextStream(
        client=gateway._client,
        model="answer-model",
        user_payload=user_payload,
        logger=gateway._logger,
    )


@pytest.mark.asyncio
async def test_answer_text_stream_yields_body_and_parses_followups() -> None:
    gateway, stream = _stream_gateway(
        [
            _delta("勾股定理："),
            _delta("a²+b²=c²"),
            _delta('<followups>["继续讲讲","证明"]</followups>'),
        ]
    )
    parts = [part async for part in _text_stream(gateway)]
    assert "".join(parts) == "勾股定理：a²+b²=c²"
    assert stream.closed is True


@pytest.mark.asyncio
async def test_answer_text_stream_splits_marker_across_delta_boundary() -> None:
    gateway, _ = _stream_gateway(
        [
            _delta("正文甲"),
            _delta("<follow"),
            _delta('ups>["追问A"]</followups>'),
        ]
    )
    answer_stream = _text_stream(gateway)
    parts = [part async for part in answer_stream]
    assert "".join(parts) == "正文甲"
    assert answer_stream.followups == ["追问A"]


@pytest.mark.asyncio
async def test_answer_text_stream_malformed_marker_drops_followups() -> None:
    gateway, _ = _stream_gateway([_delta("正文<followups>not-json</followups>")])
    answer_stream = _text_stream(gateway)
    parts = [part async for part in answer_stream]
    assert "".join(parts) == "正文"
    assert answer_stream.followups == []


@pytest.mark.asyncio
async def test_answer_text_stream_without_marker_yields_all_text() -> None:
    gateway, _ = _stream_gateway([_delta("正常"), _delta("结束")])
    answer_stream = _text_stream(gateway)
    parts = [part async for part in answer_stream]
    assert "".join(parts) == "正常结束"
    assert answer_stream.followups == []


@pytest.mark.asyncio
async def test_answer_text_stream_marker_in_isolated_delta_does_not_leak() -> None:
    """Critical 回归：<followups> 独占一个 delta（真实 tokenizer 最典型切分）。"""
    gateway, _ = _stream_gateway(
        [
            _delta("正文"),
            _delta("<followups>"),
            _delta('["追问A"]'),
            _delta("</followups>"),
        ]
    )
    answer_stream = _text_stream(gateway)
    parts = [part async for part in answer_stream]
    assert "".join(parts) == "正文"
    assert answer_stream.followups == ["追问A"]


@pytest.mark.asyncio
async def test_answer_text_stream_ignores_refusal_delta_and_marks_completed_status() -> None:
    """refusal delta 不得混入正文；completed=incomplete 暴露为 truncated。"""
    gateway, _ = _stream_gateway(
        [
            _delta("前半"),
            SimpleNamespace(type="response.refusal.delta", delta="拒绝内容"),
            SimpleNamespace(
                type="response.completed",
                response=SimpleNamespace(status="incomplete"),
            ),
        ]
    )
    answer_stream = _text_stream(gateway)
    parts = [part async for part in answer_stream]
    assert "".join(parts) == "前半"
    assert answer_stream.refused is True
    assert answer_stream.truncated is True


@pytest.mark.asyncio
async def test_answer_text_stream_fallback_for_compat_endpoint_unknown_type() -> None:
    """兼容端点未带标准 type 但含 delta 的事件按正文兜底（评审 #1 方案 B）。"""
    gateway, _ = _stream_gateway([SimpleNamespace(delta="兼容端点正文")])
    answer_stream = _text_stream(gateway)
    parts = [part async for part in answer_stream]
    assert "".join(parts) == "兼容端点正文"


@pytest.mark.asyncio
async def test_answer_text_stream_ignores_reasoning_delta() -> None:
    """reasoning 增量带 delta 字段，但不得混入回答正文。"""
    gateway, _ = _stream_gateway(
        [
            SimpleNamespace(type="response.reasoning_summary_text.delta", delta="思考过程"),
            _delta("正式正文"),
        ]
    )
    answer_stream = _text_stream(gateway)
    parts = [part async for part in answer_stream]
    assert "".join(parts) == "正式正文"


@pytest.mark.asyncio
async def test_answer_text_stream_strips_incomplete_marker_prefix_at_end() -> None:
    """自然结束于词中间（["正文", "<fol"]）：尾部标记前缀不得进入正文。"""
    gateway, _ = _stream_gateway([_delta("正文"), _delta("<fol")])
    answer_stream = _text_stream(gateway)
    parts = [part async for part in answer_stream]
    assert "".join(parts) == "正文"
    assert answer_stream.followups == []
