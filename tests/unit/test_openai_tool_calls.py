"""OpenAI 网关 tool-call 支持单元测试（memory-rebuild §2.4 D3③ / §5.7 Phase 5）。

全部使用伪造 client（伪造 ``responses.create`` 的返回），**不发起真实网络请求**。
覆盖：

- ``tools=None`` 时请求不带 ``tools``，正文 / followups / 续接契约与改造前一致；
- 流式 ``function_call`` 事件（``output_item.added`` + ``function_call_arguments.*``）
  能解析出 call_id / name / arguments，且事件不进入正文；
- ``arguments`` 坏 JSON → 该调用标记 ``executable=False`` 且不抛异常；
- 续接请求带 ``previous_response_id`` 与 ``function_call_output``；
- 工具轮 preamble 正文在流式/非流式两种模式下都保留（"已发出的 delta 一定是
  最终正文前缀"）；
- ``memory_tool_specs()`` 返回两个函数定义且 JSON Schema 合法、与 Pydantic 契约同步。
"""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from backend.conversation.contracts.errors import ModelUnavailableError
from backend.conversation.gateways import openai as gateway_module
from backend.conversation.gateways.openai import (
    AnswerTextStream,
    OpenAIGateway,
    memory_tool_specs,
)
from backend.memory.contracts.results import (
    MemoryToolReadRequest,
    MemoryToolSearchRequest,
)

ANSWER_MODEL = "answer-model"


# ---------------------------------------------------------------------------
# 伪造 SDK 对象
# ---------------------------------------------------------------------------


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
    """``response.output_text.delta``。"""
    return SimpleNamespace(type="response.output_text.delta", delta=text)


def _created(response_id: str) -> Any:
    """``response.created``（携带响应 id，续接时要用）。"""
    return SimpleNamespace(
        type="response.created",
        response=SimpleNamespace(id=response_id, status="in_progress"),
    )


def _function_call_added(
    *,
    call_id: str,
    name: str,
    item_id: str = "fc_1",
    output_index: int = 0,
    arguments: str = "",
) -> Any:
    """``response.output_item.added``（item.type == function_call）。"""
    return SimpleNamespace(
        type="response.output_item.added",
        output_index=output_index,
        item=SimpleNamespace(
            type="function_call",
            id=item_id,
            call_id=call_id,
            name=name,
            arguments=arguments,
        ),
    )


def _arguments_delta(fragment: str, *, item_id: str = "fc_1", output_index: int = 0) -> Any:
    """``response.function_call_arguments.delta``。"""
    return SimpleNamespace(
        type="response.function_call_arguments.delta",
        item_id=item_id,
        output_index=output_index,
        delta=fragment,
    )


def _arguments_done(
    arguments: str, *, name: str, item_id: str = "fc_1", output_index: int = 0
) -> Any:
    """``response.function_call_arguments.done``。"""
    return SimpleNamespace(
        type="response.function_call_arguments.done",
        item_id=item_id,
        output_index=output_index,
        sequence_number=5,
        name=name,
        arguments=arguments,
    )


def _completed(
    *,
    response_id: str = "resp_1",
    status: str = "completed",
    output: list[Any] | None = None,
) -> Any:
    """``response.completed``。"""
    return SimpleNamespace(
        type="response.completed",
        response=SimpleNamespace(id=response_id, status=status, output=output or []),
    )


def _function_call_item(*, call_id: str, name: str, arguments: str, item_id: str = "fc_1") -> Any:
    """完整响应 ``output`` 里的 function_call item。"""
    return SimpleNamespace(
        type="function_call",
        id=item_id,
        call_id=call_id,
        name=name,
        arguments=arguments,
    )


def _response(
    *,
    response_id: str = "resp_1",
    status: str = "completed",
    output_text: str | None = None,
    output: list[Any] | None = None,
) -> Any:
    """非流式完整响应。"""
    return SimpleNamespace(
        id=response_id,
        status=status,
        output_text=output_text,
        output=output or [],
        incomplete_details=None,
        refusal=None,
    )


def _stream_gateway(events: list[Any], calls: list[dict[str, Any]] | None = None) -> OpenAIGateway:
    """伪造流式网关（client.responses.create 返回固定事件流）。"""

    class FakeResponses:
        async def create(self, **kwargs: Any) -> Any:
            if calls is not None:
                calls.append(kwargs)
            return _FakeAsyncStream(events)

    class FakeClient:
        responses = FakeResponses()

    gateway = object.__new__(OpenAIGateway)
    gateway._client = FakeClient()
    gateway._settings = SimpleNamespace(
        conversation_answer_streaming=True,
        openai_answer_model=ANSWER_MODEL,
        openai_reasoning_effort="medium",
    )
    gateway._logger = gateway_module.logging.getLogger("test.openai.tool_calls")
    return gateway


def _structured_gateway(responses: list[Any], calls: list[dict[str, Any]]) -> OpenAIGateway:
    """伪造非流式网关（按顺序返回给定完整响应）。"""

    class FakeResponses:
        async def create(self, **kwargs: Any) -> Any:
            calls.append(kwargs)
            return responses.pop(0)

    class FakeClient:
        responses = FakeResponses()

    gateway = object.__new__(OpenAIGateway)
    gateway._client = FakeClient()
    gateway._settings = SimpleNamespace(
        openai_answer_model=ANSWER_MODEL,
        openai_reasoning_effort="medium",
    )
    gateway._logger = gateway_module.logging.getLogger("test.openai.tool_calls")
    return gateway


async def _drain(stream: AnswerTextStream) -> str:
    parts = [part async for part in stream]
    return "".join(parts)


# ---------------------------------------------------------------------------
# tools=None：请求与行为零变化
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_without_tools_keeps_request_and_answer_contract() -> None:
    calls: list[dict[str, Any]] = []
    gateway = _stream_gateway([_delta("正文"), _delta('<followups>["追问"]</followups>')], calls)

    stream = gateway.open_answer_stream(answer_context={"question": "q"})
    text = await _drain(stream)

    assert text == "正文"
    assert stream.followups == ["追问"]
    assert stream.pending_tool_calls == []
    assert stream.response_id is None
    assert stream.tool_rounds == 0
    request = calls[0]
    # 改造前的请求形状：一个键都不多、一个键都不少。
    assert set(request) == {
        "model",
        "input",
        "max_output_tokens",
        "timeout",
        "reasoning",
        "stream",
    }
    assert request["stream"] is True
    assert request["input"][0]["role"] == "system"
    assert request["input"][1] == {"role": "user", "content": '{"question": "q"}'}
    # 工具引导词不得出现在未启用工具的提示词里。
    assert "memory_search" not in request["input"][0]["content"]


@pytest.mark.asyncio
async def test_non_streaming_without_tools_returns_tuple_and_omits_tools() -> None:
    calls: list[dict[str, Any]] = []
    gateway = _structured_gateway(
        [_response(output_text='{"answer":"回答","followups":[]}')], calls
    )

    deltas, payload = await gateway.stream_answer(answer_context={"question": "q"})

    assert "".join(deltas) == "回答"
    assert payload == {"answer": "回答", "followups": []}
    assert "tools" not in calls[0]
    assert "previous_response_id" not in calls[0]


# ---------------------------------------------------------------------------
# 流式工具请求解析
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_parses_function_call_arguments_across_events() -> None:
    calls: list[dict[str, Any]] = []
    gateway = _stream_gateway(
        [
            _created("resp_tool"),
            _function_call_added(call_id="call_1", name="memory_search"),
            _arguments_delta('{"queries":'),
            _arguments_delta('["椭圆的掌握情况"]}'),
            _arguments_done('{"queries":["椭圆的掌握情况"]}', name="memory_search"),
            _completed(response_id="resp_tool"),
        ],
        calls,
    )

    stream = gateway.open_answer_stream(answer_context={"question": "q"}, tools=memory_tool_specs())
    text = await _drain(stream)

    # 工具调用事件绝不进入正文。
    assert text == ""
    assert stream.pending_tool_calls == [
        {
            "call_id": "call_1",
            "name": "memory.search",
            "arguments": {"queries": ["椭圆的掌握情况"]},
            "executable": True,
        }
    ]
    assert stream.response_id == "resp_tool"
    assert stream.tool_rounds == 1
    # 启用工具时下发函数定义并拼接引导词。
    request = calls[0]
    assert [tool["name"] for tool in request["tools"]] == ["memory_search", "memory_read"]
    assert "memory_search" in request["input"][0]["content"]


@pytest.mark.asyncio
async def test_stream_keeps_parallel_function_calls_in_event_order() -> None:
    gateway = _stream_gateway(
        [
            _function_call_added(call_id="call_1", name="memory_search", output_index=0),
            _arguments_done(
                '{"queries":["极限"]}', name="memory_search", output_index=0, item_id="fc_1"
            ),
            _function_call_added(
                call_id="call_2", name="memory_read", item_id="fc_2", output_index=1
            ),
            _arguments_done(
                '{"memory_id":"mastery:极限"}',
                name="memory_read",
                output_index=1,
                item_id="fc_2",
            ),
            _completed(),
        ]
    )

    stream = gateway.open_answer_stream(answer_context={"question": "q"}, tools=memory_tool_specs())
    await _drain(stream)

    assert [call["call_id"] for call in stream.pending_tool_calls] == ["call_1", "call_2"]
    assert [call["name"] for call in stream.pending_tool_calls] == [
        "memory.search",
        "memory.read",
    ]
    assert stream.pending_tool_calls[1]["arguments"] == {"memory_id": "mastery:极限"}


@pytest.mark.asyncio
async def test_stream_yields_tool_turn_preamble_and_still_reports_calls() -> None:
    """工具轮里模型先说的正文必须照常 yield（调用方按轮次拼接）。"""
    gateway = _stream_gateway(
        [
            _delta("我先查一下你的学习记录。"),
            _function_call_added(call_id="call_1", name="memory_search"),
            _arguments_done('{"queries":["椭圆"]}', name="memory_search"),
            _completed(),
        ]
    )

    stream = gateway.open_answer_stream(answer_context={"question": "q"}, tools=memory_tool_specs())
    text = await _drain(stream)

    assert text == "我先查一下你的学习记录。"
    assert stream.full_text == "我先查一下你的学习记录。"
    assert stream.pending_tool_calls[0]["executable"] is True
    assert stream.tool_rounds == 1


@pytest.mark.asyncio
async def test_stream_bad_json_arguments_are_marked_not_executable() -> None:
    gateway = _stream_gateway(
        [
            _function_call_added(call_id="call_1", name="memory_read"),
            _arguments_done("{不是 JSON", name="memory_read"),
            _delta("正文不受影响"),
            _completed(),
        ]
    )

    stream = gateway.open_answer_stream(answer_context={"question": "q"}, tools=memory_tool_specs())
    text = await _drain(stream)

    # 坏参数不抛异常，也不影响同一轮的正文。
    assert text == "正文不受影响"
    (call,) = stream.pending_tool_calls
    assert call["call_id"] == "call_1"
    assert call["name"] == "memory.read"
    assert call["arguments"] == {}
    assert call["executable"] is False
    assert call["arguments_raw"] == "{不是 JSON"
    assert str(call["error"]).startswith("invalid_json")


@pytest.mark.asyncio
async def test_stream_non_object_arguments_and_missing_call_id_are_not_executable() -> None:
    gateway = _stream_gateway(
        [
            _function_call_added(call_id="call_1", name="memory_search", output_index=0),
            _arguments_done('["椭圆"]', name="memory_search", output_index=0),
            _function_call_added(call_id="", name="memory_search", item_id="fc_2", output_index=1),
            _arguments_done(
                '{"queries":["椭圆"]}', name="memory_search", output_index=1, item_id="fc_2"
            ),
            _completed(),
        ]
    )

    stream = gateway.open_answer_stream(answer_context={"question": "q"}, tools=memory_tool_specs())
    await _drain(stream)

    first, second = stream.pending_tool_calls
    assert first["executable"] is False
    assert first["error"] == "arguments_not_object"
    assert second["executable"] is False
    assert second["error"] == "missing_call_id"


@pytest.mark.asyncio
async def test_stream_empty_arguments_are_treated_as_empty_object() -> None:
    gateway = _stream_gateway(
        [
            _function_call_added(call_id="call_1", name="memory_search"),
            _completed(),
        ]
    )

    stream = gateway.open_answer_stream(answer_context={"question": "q"}, tools=memory_tool_specs())
    await _drain(stream)

    (call,) = stream.pending_tool_calls
    assert call["arguments"] == {}
    assert call["executable"] is True


@pytest.mark.asyncio
async def test_stream_pending_tool_calls_property_returns_copy() -> None:
    gateway = _stream_gateway(
        [
            _function_call_added(call_id="call_1", name="memory_search"),
            _arguments_done('{"queries":["椭圆"]}', name="memory_search"),
            _completed(),
        ]
    )

    stream = gateway.open_answer_stream(answer_context={"question": "q"}, tools=memory_tool_specs())
    await _drain(stream)

    calls = stream.pending_tool_calls
    calls.clear()
    assert len(stream.pending_tool_calls) == 1


@pytest.mark.asyncio
async def test_stream_recovers_tool_calls_embedded_in_completed_event() -> None:
    """兼容端点：只在终态事件里带回完整 output。"""
    gateway = _stream_gateway(
        [
            _completed(
                output=[
                    _function_call_item(
                        call_id="call_9", name="memory_search", arguments='{"queries":["椭圆"]}'
                    )
                ]
            )
        ]
    )

    stream = gateway.open_answer_stream(answer_context={"question": "q"}, tools=memory_tool_specs())
    await _drain(stream)

    (call,) = stream.pending_tool_calls
    assert call["call_id"] == "call_9"
    assert call["name"] == "memory.search"
    assert call["arguments"] == {"queries": ["椭圆"]}


# ---------------------------------------------------------------------------
# 续接请求
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_continuation_sends_previous_response_id_and_tool_output() -> None:
    calls: list[dict[str, Any]] = []
    gateway = _stream_gateway([_delta("续接正文"), _completed(response_id="resp_2")], calls)

    stream = gateway.open_answer_stream(
        answer_context={"question": "q"},
        tools=memory_tool_specs(),
        previous_response_id="resp_1",
        tool_outputs=[{"call_id": "call_1", "name": "memory.search", "output": '{"items": []}'}],
        tool_rounds=2,
    )
    text = await _drain(stream)

    assert text == "续接正文"
    request = calls[0]
    assert request["previous_response_id"] == "resp_1"
    assert request["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": '{"items": []}'}
    ]
    # 续接不重复下发 system/user（上一轮上下文由 previous_response_id 携带）。
    assert all(item.get("role") is None for item in request["input"])
    assert request["tools"][0]["name"] == "memory_search"
    # 续接参数自检：轮数由调用方注入，本轮没有新工具调用，不增加。
    assert stream.tool_rounds == 2
    assert stream.response_id == "resp_2"
    assert stream.pending_tool_calls == []


@pytest.mark.asyncio
async def test_stream_continuation_serializes_dict_tool_output() -> None:
    calls: list[dict[str, Any]] = []
    gateway = _stream_gateway([_completed()], calls)

    stream = gateway.open_answer_stream(
        answer_context={"question": "q"},
        tools=memory_tool_specs(),
        previous_response_id="resp_1",
        tool_outputs=[{"call_id": "call_1", "name": "memory.search", "output": {"items": []}}],
    )
    await _drain(stream)

    assert calls[0]["input"][0]["output"] == '{"items": []}'


@pytest.mark.asyncio
async def test_stream_ignores_tool_output_without_call_id() -> None:
    calls: list[dict[str, Any]] = []
    gateway = _stream_gateway([_delta("正文"), _completed()], calls)

    stream = gateway.open_answer_stream(
        answer_context={"question": "q"},
        tools=memory_tool_specs(),
        previous_response_id="resp_1",
        tool_outputs=[
            {"name": "memory.search", "output": "{}"},
            {"call_id": "call_2", "name": "memory.search", "output": "{}"},
        ],
    )
    text = await _drain(stream)

    assert text == "正文"
    assert calls[0]["input"] == [
        {"type": "function_call_output", "call_id": "call_2", "output": "{}"}
    ]


@pytest.mark.asyncio
async def test_open_answer_stream_rejects_incomplete_continuation() -> None:
    gateway = _stream_gateway([])

    with pytest.raises(ModelUnavailableError):
        gateway.open_answer_stream(
            answer_context={"question": "q"},
            tools=memory_tool_specs(),
            previous_response_id="resp_1",
        )
    with pytest.raises(ModelUnavailableError):
        gateway.open_answer_stream(
            answer_context={"question": "q"},
            tools=memory_tool_specs(),
            tool_outputs=[{"call_id": "call_1", "output": "{}"}],
        )


@pytest.mark.asyncio
async def test_stream_continuation_without_usable_output_degrades() -> None:
    """所有工具结果都缺 call_id 时不做无 input 的续接请求，直接降级。"""
    calls: list[dict[str, Any]] = []
    gateway = _stream_gateway([_delta("不应到达")], calls)

    stream = gateway.open_answer_stream(
        answer_context={"question": "q"},
        tools=memory_tool_specs(),
        previous_response_id="resp_1",
        tool_outputs=[{"name": "memory.search", "output": "{}"}],
    )
    with pytest.raises(ModelUnavailableError):
        await _drain(stream)
    assert calls == []


@pytest.mark.asyncio
async def test_stream_continuation_streaming_failure_is_mapped() -> None:
    """续接请求本身失败（超时）也统一映射为 ModelUnavailableError。"""

    class FakeResponses:
        async def create(self, **kwargs: Any) -> Any:
            raise TimeoutError("超时")

    class FakeClient:
        responses = FakeResponses()

    gateway = _stream_gateway([])
    gateway._client = FakeClient()

    stream = gateway.open_answer_stream(
        answer_context={"question": "q"},
        tools=memory_tool_specs(),
        previous_response_id="resp_1",
        tool_outputs=[{"call_id": "call_1", "output": "{}"}],
    )
    with pytest.raises(ModelUnavailableError):
        await _drain(stream)


# ---------------------------------------------------------------------------
# 非流式工具轮
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_streaming_tool_turn_keeps_preamble_and_returns_calls() -> None:
    calls: list[dict[str, Any]] = []
    preamble = '{"answer":"我先查一下你的学习记录。","followups":[]}'
    gateway = _structured_gateway(
        [
            _response(
                response_id="resp_tool",
                output_text=preamble,
                output=[
                    SimpleNamespace(
                        type="message",
                        content=[SimpleNamespace(type="output_text", text=preamble)],
                    ),
                    _function_call_item(
                        call_id="call_1",
                        name="memory_search",
                        arguments='{"queries":["椭圆"]}',
                    ),
                ],
            )
        ],
        calls,
    )

    turn = await gateway.stream_answer(answer_context={"question": "q"}, tools=memory_tool_specs())

    assert not isinstance(turn, tuple)
    assert "".join(turn.deltas) == "我先查一下你的学习记录。"
    assert turn.payload == {"answer": "我先查一下你的学习记录。", "followups": []}
    assert turn.pending_tool_calls[0]["name"] == "memory.search"
    assert turn.pending_tool_calls[0]["executable"] is True
    assert turn.response_id == "resp_tool"
    assert len(calls) == 1  # 工具轮不重试
    assert calls[0]["tools"][0]["name"] == "memory_search"


@pytest.mark.asyncio
async def test_non_streaming_tool_turn_keeps_plain_preamble_text() -> None:
    """兼容端点给出非 JSON preamble 时原样保留（不丢正文）。"""
    calls: list[dict[str, Any]] = []
    gateway = _structured_gateway(
        [
            _response(
                output_text="我先查一下你的学习记录。",
                output=[
                    _function_call_item(
                        call_id="call_1", name="memory_read", arguments='{"memory_id":"m1"}'
                    )
                ],
            )
        ],
        calls,
    )

    turn = await gateway.stream_answer(answer_context={"question": "q"}, tools=memory_tool_specs())

    assert "".join(turn.deltas) == "我先查一下你的学习记录。"
    assert turn.payload == {"answer": "我先查一下你的学习记录。", "followups": []}
    assert turn.pending_tool_calls[0]["name"] == "memory.read"


@pytest.mark.asyncio
async def test_non_streaming_tool_turn_without_preamble_has_empty_answer() -> None:
    calls: list[dict[str, Any]] = []
    gateway = _structured_gateway(
        [
            _response(
                output=[
                    _function_call_item(
                        call_id="call_1", name="memory_search", arguments='{"queries":["椭圆"]}'
                    )
                ]
            )
        ],
        calls,
    )

    turn = await gateway.stream_answer(answer_context={"question": "q"}, tools=memory_tool_specs())

    assert turn.deltas == []
    assert turn.payload == {"answer": "", "followups": []}
    assert len(turn.pending_tool_calls) == 1


@pytest.mark.asyncio
async def test_non_streaming_tool_turn_with_direct_answer_has_no_calls() -> None:
    calls: list[dict[str, Any]] = []
    gateway = _structured_gateway(
        [_response(output_text='{"answer":"不需要查记忆","followups":["追问"]}')], calls
    )

    turn = await gateway.stream_answer(answer_context={"question": "q"}, tools=memory_tool_specs())

    assert "".join(turn.deltas) == "不需要查记忆"
    assert turn.payload == {"answer": "不需要查记忆", "followups": ["追问"]}
    assert turn.pending_tool_calls == []
    assert turn.response_id == "resp_1"


@pytest.mark.asyncio
async def test_non_streaming_tool_continuation_sends_previous_response_id() -> None:
    calls: list[dict[str, Any]] = []
    gateway = _structured_gateway(
        [_response(output_text='{"answer":"续接后的回答","followups":[]}')], calls
    )

    turn = await gateway.stream_answer(
        answer_context={"question": "q"},
        tools=memory_tool_specs(),
        previous_response_id="resp_1",
        tool_outputs=[{"call_id": "call_1", "name": "memory.search", "output": "{}"}],
    )

    assert "".join(turn.deltas) == "续接后的回答"
    request = calls[0]
    assert request["previous_response_id"] == "resp_1"
    assert request["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "{}"}
    ]


# ---------------------------------------------------------------------------
# memory_tool_specs()
# ---------------------------------------------------------------------------

_SCHEMA_TYPES = {"object", "array", "string", "integer", "number", "boolean", "null"}


def _assert_valid_schema(schema: dict[str, Any]) -> None:
    """无第三方依赖的 JSON Schema 结构校验（类型、required 子集、边界自洽）。"""
    assert schema.get("type") in _SCHEMA_TYPES, schema
    if schema["type"] == "object":
        properties = schema.get("properties")
        assert isinstance(properties, dict) and properties, schema
        assert set(schema.get("required", [])) <= set(properties), schema
        for prop in properties.values():
            _assert_valid_schema(prop)
        return
    if schema["type"] == "array":
        items = schema.get("items")
        assert isinstance(items, dict), schema
        _assert_valid_schema(items)
        assert schema["minItems"] <= schema["maxItems"], schema
        return
    if "enum" in schema:
        assert schema["enum"] and all(isinstance(item, str) for item in schema["enum"]), schema
    if "minimum" in schema and "maximum" in schema:
        assert schema["minimum"] <= schema["maximum"], schema
    for key in ("minimum", "maximum"):
        if key in schema:
            assert isinstance(schema[key], int), schema


def test_memory_tool_specs_returns_two_function_definitions() -> None:
    specs = memory_tool_specs()

    assert [spec["type"] for spec in specs] == ["function", "function"]
    # 线上函数名必须是 ``[A-Za-z0-9_-]``（点号会被服务端拒绝）。
    assert [spec["name"] for spec in specs] == ["memory_search", "memory_read"]
    for spec in specs:
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", spec["name"])
        assert spec["description"]
        assert spec["strict"] is False
        _assert_valid_schema(spec["parameters"])

    # 每次调用返回新副本，调用方改动不会污染后续调用。
    specs[0]["name"] = "mutated"
    assert memory_tool_specs()[0]["name"] == "memory_search"


@pytest.mark.parametrize(
    ("tool_name", "contract"),
    [
        ("memory_search", MemoryToolSearchRequest),
        ("memory_read", MemoryToolReadRequest),
    ],
)
def test_memory_tool_specs_match_pydantic_contract(
    tool_name: str, contract: type[BaseModel]
) -> None:
    """工具 JSON Schema 与服务端 Pydantic 契约（memory/contracts/results.py）同步。"""
    spec = next(item for item in memory_tool_specs() if item["name"] == tool_name)
    parameters = spec["parameters"]
    contract_schema = contract.model_json_schema()

    assert parameters["additionalProperties"] is False
    assert parameters["required"] == contract_schema["required"]
    for name, prop in parameters["properties"].items():
        expected = contract_schema["properties"][name]
        for key in (
            "type",
            "enum",
            "minimum",
            "maximum",
            "minItems",
            "maxItems",
            "minLength",
            "maxLength",
        ):
            if key in prop:
                assert prop[key] == expected.get(key), (tool_name, name, key)
        if prop["type"] == "array":
            assert prop["items"]["minLength"] == expected["items"]["minLength"]
            assert prop["items"]["maxLength"] == expected["items"]["maxLength"]
