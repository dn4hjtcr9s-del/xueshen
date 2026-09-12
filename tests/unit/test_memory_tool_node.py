"""memory_tool 节点单测（memory-rebuild §5.7 Phase 5）。

覆盖工具循环的四条硬约束与两条降级语义：

- 有界：调用数上限 6（`MEMORY_TOOL_CALL_BUDGET`）、轮数上限 6，超限回
  `MEMORY_TOOL_BUDGET_EXCEEDED` 且不再执行；
- 幂等：同一 turn 内相同 (tool, 规范化参数) 复用结果，**不重复消耗预算**；
- 裁剪：超限结果置 `truncated=true` 并附下一步可用的 read hint；
- 可审计：每次调用/结果写 rollout 摘要记录，**正文不落 rollout**；
- 错误分类：401/403 让 Turn 失败；其余（含 404）交回模型自行纠错；
- 引用：`memory.read` 结果登记 version/checksum/行范围，供 finalize 回填。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from backend.conversation.contracts.errors import MemoryUnavailableError
from backend.conversation.graph.nodes import memory_tool as node
from backend.conversation.graph.nodes.memory_tool import (
    MEMORY_TOOL_CALL_BUDGET,
    run_memory_tools,
    should_continue_memory_tools,
)
from tests.conversation.graph_fixtures import build_runtime


class FakeMemoryToolsGateway:
    """脚本化记忆工具网关：记录每次调用，可按序注入异常。"""

    def __init__(self) -> None:
        self.search_results: list[dict[str, Any]] = []
        self.read_results: list[dict[str, Any]] = []
        self.errors: list[Exception] = []
        self.calls: list[dict[str, Any]] = []

    async def search_memories(
        self,
        *,
        queries: list[str],
        match_mode: str = "any",
        max_results: int = 10,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "tool": "memory.search",
                "queries": queries,
                "match_mode": match_mode,
                "max_results": max_results,
            }
        )
        if self.errors:
            raise self.errors.pop(0)
        if not self.search_results:
            return {"items": [], "truncated": False}
        return self.search_results.pop(0)

    async def read_memory(
        self,
        *,
        memory_id: str,
        line_offset: int = 0,
        max_lines: int = 200,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "tool": "memory.read",
                "memory_id": memory_id,
                "line_offset": line_offset,
                "max_lines": max_lines,
            }
        )
        if self.errors:
            raise self.errors.pop(0)
        if not self.read_results:
            return _read_payload(memory_id=memory_id, content="正文")
        return self.read_results.pop(0)


class FakeRecorder:
    """记录 rollout 调用（真实 recorder 的行为在 test_rollout_recorder.py 覆盖）。"""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, Any]]] = []

    async def record(
        self, record_type: str, *, payload: dict[str, Any], turn_id: Any = None
    ) -> bool:
        self.records.append((record_type, payload))
        return True


def _read_payload(
    *,
    memory_id: str = "mastery:椭圆",
    content: str = "椭圆正文",
    version: int = 3,
    checksum: str = "a" * 64,
    line_offset: int = 0,
    total_lines: int = 1,
    truncated: bool = False,
) -> dict[str, Any]:
    return {
        "memory_id": memory_id,
        "version": version,
        "checksum": checksum,
        "content": content,
        "line_offset": line_offset,
        "total_lines": total_lines,
        "truncated": truncated,
    }


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "turn_id": "turn-1",
        "user_id": "user-1",
        "memory_pending_tool_calls": [],
    }
    state.update(overrides)
    return state


def _runtime(**overrides: Any) -> Any:
    gateway = overrides.pop("memory_gateway", FakeMemoryToolsGateway())
    runtime = build_runtime(memory_gateway=gateway, **overrides)
    runtime.rollout_recorder = overrides.get("recorder", FakeRecorder())
    return runtime


def _call(
    name: str,
    arguments: dict[str, Any],
    *,
    call_id: str = "call-1",
) -> dict[str, Any]:
    return {"call_id": call_id, "name": name, "arguments": arguments}


# ---------------------------------------------------------------------------
# 基本执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_result_is_returned_and_counted() -> None:
    gateway = FakeMemoryToolsGateway()
    gateway.search_results = [
        {
            "items": [
                {
                    "memory_id": "mastery:椭圆",
                    "name": "椭圆",
                    "description": "圆锥曲线之一",
                    "keywords": ["焦点"],
                    "version": 3,
                    "updated_at": "2026-09-10T05:00:00Z",
                }
            ],
            "truncated": False,
        }
    ]
    runtime = _runtime(memory_gateway=gateway)
    result = await run_memory_tools(
        _state(memory_pending_tool_calls=[_call("memory.search", {"queries": ["椭圆"]})]),
        runtime=runtime,
    )

    assert result["memory_tool_calls"] == 1
    assert result["memory_tool_rounds"] == 1
    assert result["memory_pending_tool_calls"] == []
    assert result["memory_truncated"] is False
    assert result["degraded_flags"] == []
    assert gateway.calls[0]["queries"] == ["椭圆"]
    output = result["memory_tool_outputs"][0]
    assert output["call_id"] == "call-1"
    # 结果文本是 JSON（模型可直接读），且带 memory_id
    assert "mastery:椭圆" in output["output"]


@pytest.mark.asyncio
async def test_read_registers_citation_with_version_and_checksum() -> None:
    gateway = FakeMemoryToolsGateway()
    gateway.read_results = [
        _read_payload(line_offset=0, total_lines=40, content="第一段", truncated=True)
    ]
    runtime = _runtime(memory_gateway=gateway)
    result = await run_memory_tools(
        _state(
            memory_pending_tool_calls=[
                _call("memory.read", {"memory_id": "mastery:椭圆", "max_lines": 1})
            ]
        ),
        runtime=runtime,
    )

    citations = result["memory_citations"]
    assert len(citations) == 1
    assert citations[0]["memory_id"] == "mastery:椭圆"
    assert citations[0]["version"] == 3
    assert citations[0]["checksum"] == "a" * 64
    assert citations[0]["total_lines"] == 40
    assert citations[0]["truncated"] is True


@pytest.mark.asyncio
async def test_unknown_tool_returns_error_without_touching_gateway() -> None:
    gateway = FakeMemoryToolsGateway()
    runtime = _runtime(memory_gateway=gateway)
    result = await run_memory_tools(
        _state(memory_pending_tool_calls=[_call("memory.write", {})]),
        runtime=runtime,
    )

    assert gateway.calls == []
    assert result["memory_tool_calls"] == 0
    assert "MEMORY_TOOL_UNKNOWN" in result["memory_tool_outputs"][0]["output"]


# ---------------------------------------------------------------------------
# 有界：调用上限与轮数上限
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_blocks_the_seventh_call() -> None:
    gateway = FakeMemoryToolsGateway()
    runtime = _runtime(memory_gateway=gateway)
    pending = [
        _call("memory.search", {"queries": [f"关键词{i}"]}, call_id=f"call-{i}")
        for i in range(MEMORY_TOOL_CALL_BUDGET + 1)
    ]

    result = await run_memory_tools(_state(memory_pending_tool_calls=pending), runtime=runtime)

    # 只执行 6 次（第 7 次不碰网关），并给出 budget_exceeded 结果
    assert result["memory_tool_calls"] == MEMORY_TOOL_CALL_BUDGET
    assert len(gateway.calls) == MEMORY_TOOL_CALL_BUDGET
    assert "memory_tool_budget_exceeded" in result["degraded_flags"]
    last = result["memory_tool_outputs"][-1]
    assert "MEMORY_TOOL_BUDGET_EXCEEDED" in last["output"]
    # 超限调用没有合法 call_index（契约 ∈[1,6]），只留 result 记录
    tool_calls = [r for r in result["memory_tool_records"] if r["status"] == "ok"]
    assert len(tool_calls) == MEMORY_TOOL_CALL_BUDGET


@pytest.mark.asyncio
async def test_budget_exhausted_at_entry_does_not_call_gateway() -> None:
    gateway = FakeMemoryToolsGateway()
    runtime = _runtime(memory_gateway=gateway)
    result = await run_memory_tools(
        _state(
            memory_tool_calls=MEMORY_TOOL_CALL_BUDGET,
            memory_pending_tool_calls=[_call("memory.search", {"queries": ["椭圆"]})],
        ),
        runtime=runtime,
    )

    assert gateway.calls == []
    assert "MEMORY_TOOL_BUDGET_EXCEEDED" in result["memory_tool_outputs"][0]["output"]


def test_should_continue_respects_call_budget_and_round_cap() -> None:
    call = [_call("memory.search", {"queries": ["椭圆"]})]
    assert should_continue_memory_tools({"memory_pending_tool_calls": call}) is True
    assert should_continue_memory_tools({}) is False
    # 调用数用尽
    assert (
        should_continue_memory_tools(
            {"memory_pending_tool_calls": call, "memory_tool_calls": MEMORY_TOOL_CALL_BUDGET}
        )
        is False
    )
    # 轮数用尽（全部命中缓存时调用数不增长，只能靠轮数兜底）
    assert (
        should_continue_memory_tools(
            {
                "memory_pending_tool_calls": call,
                "memory_tool_calls": 1,
                "memory_tool_rounds": MEMORY_TOOL_CALL_BUDGET,
            }
        )
        is False
    )


# ---------------------------------------------------------------------------
# 幂等：同参数复用结果、不重复消耗预算
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_arguments_are_served_from_cache() -> None:
    gateway = FakeMemoryToolsGateway()
    runtime = _runtime(memory_gateway=gateway)
    first = await run_memory_tools(
        _state(memory_pending_tool_calls=[_call("memory.search", {"queries": ["椭圆"]})]),
        runtime=runtime,
    )
    # 第二轮：参数顺序不同但语义相同 → 命中缓存
    second = await run_memory_tools(
        _state(
            memory_pending_tool_calls=[
                _call(
                    "memory.search",
                    {"match_mode": "any", "queries": ["椭圆"], "max_results": 10},
                    call_id="call-2",
                )
            ],
            memory_tool_records=first["memory_tool_records"],
            memory_tool_calls=first["memory_tool_calls"],
            memory_tool_rounds=1,
        ),
        runtime=runtime,
    )

    assert len(gateway.calls) == 1  # 第二次没有真的打网关
    assert second["memory_tool_calls"] == 1  # 预算没有被重复消耗
    assert second["memory_tool_outputs"][0]["call_id"] == "call-2"
    assert second["memory_tool_outputs"][0]["output"] == first["memory_tool_outputs"][0]["output"]


@pytest.mark.asyncio
async def test_query_whitespace_and_order_are_normalized_for_cache_key() -> None:
    gateway = FakeMemoryToolsGateway()
    runtime = _runtime(memory_gateway=gateway)
    first = await run_memory_tools(
        _state(memory_pending_tool_calls=[_call("memory.search", {"queries": [" 椭圆 "]})]),
        runtime=runtime,
    )
    assert gateway.calls[0]["queries"] == ["椭圆"]
    second = await run_memory_tools(
        _state(
            memory_pending_tool_calls=[_call("memory.search", {"queries": ["椭圆"]})],
            memory_tool_records=first["memory_tool_records"],
            memory_tool_calls=1,
        ),
        runtime=runtime,
    )
    assert len(gateway.calls) == 1
    assert second["memory_tool_calls"] == 1


# ---------------------------------------------------------------------------
# 裁剪
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_single_line_result_gets_no_executable_offset_hint() -> None:
    """单行超长正文连一行都放不下：不给可执行 offset，改"勿重试"语义（新发现 11）。

    此时 `consumed + delivered == consumed`（delivered=0），旧实现会把提示写回**请求的
    同一 offset**，模型照做只会命中幂等缓存、白白浪费剩余轮数。
    """
    gateway = FakeMemoryToolsGateway()
    gateway.read_results = [
        _read_payload(content="x" * (node.RESULT_MAX_CHARS + 1000), total_lines=1)
    ]
    runtime = _runtime(memory_gateway=gateway)
    result = await run_memory_tools(
        _state(
            memory_pending_tool_calls=[
                _call("memory.read", {"memory_id": "mastery:椭圆", "max_lines": 200})
            ]
        ),
        runtime=runtime,
    )

    assert result["memory_truncated"] is True
    assert "memory_tool_truncated" in result["degraded_flags"]
    output = result["memory_tool_outputs"][0]["output"]
    assert len(output) <= node.RESULT_MAX_CHARS + 1000
    payload = json.loads(output)  # 仍是合法 JSON
    assert payload["truncated"] is True
    assert payload.get("reason") == "memory_tool_budget_exhausted"
    hint = payload["hint"]
    assert "line_offset" not in hint, "连一行都放不下时不得给出可执行的 offset 提示"
    assert hint["retryable"] is False
    assert "勿" in hint["note"]


@pytest.mark.asyncio
async def test_long_result_is_cut_to_remaining_token_budget() -> None:
    """预算能吃下若干行时：正文按行裁到预算内，并给出指向**已投递**下一行的 offset。"""
    gateway = FakeMemoryToolsGateway()
    gateway.read_results = [
        _read_payload(content="\n".join(f"第 {index} 行内容" for index in range(500)))
    ]
    runtime = _runtime(memory_gateway=gateway)
    runtime.settings = runtime.settings.model_copy(update={"conversation_memory_token_budget": 120})
    result = await run_memory_tools(
        _state(
            memory_pending_tool_calls=[
                _call("memory.read", {"memory_id": "mastery:椭圆", "max_lines": 200})
            ]
        ),
        runtime=runtime,
    )

    assert result["memory_truncated"] is True
    assert "memory_tool_truncated" in result["degraded_flags"]
    output = result["memory_tool_outputs"][0]["output"]
    # 正文被裁到剩余预算以内；续读提示是裁剪**之后**追加的固定小额开销，
    # 因此总量 = 预算 + 提示（提示本身有固定上限，不会随正文增长）。
    assert runtime.token_counter.count(output) <= 120 + 64
    payload = json.loads(output)
    delivered = str(payload["content"]).splitlines()
    assert 0 < len(delivered) < 500, "预算应当真的裁掉了后面的行"
    assert payload["hint"]["line_offset"] == len(delivered)
    assert '"truncated": true' in output


@pytest.mark.asyncio
async def test_exhausted_token_budget_returns_minimal_stub() -> None:
    gateway = FakeMemoryToolsGateway()
    gateway.read_results = [_read_payload(content=" ".join(["词"] * 500))]
    runtime = _runtime(memory_gateway=gateway)
    runtime.settings = runtime.settings.model_copy(update={"conversation_memory_token_budget": 50})
    # 预算已被上一轮工具结果吃光（记录里带 token 快照）
    result = await run_memory_tools(
        _state(
            memory_tool_records=[
                {"call_id": "old", "tool": "memory.read", "status": "ok", "output_tokens": 50}
            ],
            memory_pending_tool_calls=[
                _call("memory.read", {"memory_id": "mastery:椭圆", "max_lines": 200})
            ],
        ),
        runtime=runtime,
    )

    assert result["memory_truncated"] is True
    assert "memory_tool_budget_exhausted" in result["memory_tool_outputs"][0]["output"]


# ---------------------------------------------------------------------------
# 错误分类
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_failure_fails_the_turn() -> None:
    gateway = FakeMemoryToolsGateway()
    gateway.errors = [MemoryUnavailableError("拒绝", source_http_status=401)]
    runtime = _runtime(memory_gateway=gateway)

    with pytest.raises(MemoryUnavailableError):
        await run_memory_tools(
            _state(memory_pending_tool_calls=[_call("memory.read", {"memory_id": "m"})]),
            runtime=runtime,
        )


@pytest.mark.asyncio
async def test_not_found_is_returned_to_the_model() -> None:
    gateway = FakeMemoryToolsGateway()
    gateway.errors = [MemoryUnavailableError("不存在", source_http_status=404)]
    runtime = _runtime(memory_gateway=gateway)
    result = await run_memory_tools(
        _state(memory_pending_tool_calls=[_call("memory.read", {"memory_id": "m"})]),
        runtime=runtime,
    )

    assert "MEMORY_TOOL_NOT_FOUND" in result["memory_tool_outputs"][0]["output"]
    assert "memory_tool_degraded" in result["degraded_flags"]
    assert result["memory_tool_calls"] == 1


@pytest.mark.asyncio
async def test_server_error_degrades_without_blocking_answer() -> None:
    gateway = FakeMemoryToolsGateway()
    gateway.errors = [MemoryUnavailableError("不可用", source_http_status=503)]
    runtime = _runtime(memory_gateway=gateway)
    result = await run_memory_tools(
        _state(memory_pending_tool_calls=[_call("memory.search", {"queries": ["椭圆"]})]),
        runtime=runtime,
    )

    assert "MEMORY_TOOL_UNAVAILABLE" in result["memory_tool_outputs"][0]["output"]
    assert "memory_tool_degraded" in result["degraded_flags"]
    # 失败不写 citation
    assert result["memory_citations"] == []


# ---------------------------------------------------------------------------
# 可审计
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rollout_records_carry_summary_not_body() -> None:
    recorder = FakeRecorder()
    gateway = FakeMemoryToolsGateway()
    gateway.read_results = [_read_payload(content="很长的正文" * 50, version=7)]
    runtime = _runtime(memory_gateway=gateway, recorder=recorder)
    await run_memory_tools(
        _state(memory_pending_tool_calls=[_call("memory.read", {"memory_id": "mastery:椭圆"})]),
        runtime=runtime,
    )

    types = [record_type for record_type, _ in recorder.records]
    assert types == ["memory_tool_call", "memory_tool_result"]
    call_payload = recorder.records[0][1]
    assert call_payload["tool"] == "memory.read"
    assert call_payload["call_index"] == 1
    result_payload = recorder.records[1][1]
    assert result_payload["status"] == "ok"
    assert result_payload["document_versions"] == ["7"]
    assert result_payload["result_count"] == 1
    # 正文绝不落 rollout
    assert "很长的正文" not in str(recorder.records)


@pytest.mark.asyncio
async def test_budget_exceeded_only_writes_result_record() -> None:
    recorder = FakeRecorder()
    runtime = _runtime(recorder=recorder)
    await run_memory_tools(
        _state(
            memory_tool_calls=MEMORY_TOOL_CALL_BUDGET,
            memory_pending_tool_calls=[_call("memory.search", {"queries": ["椭圆"]})],
        ),
        runtime=runtime,
    )

    types = [record_type for record_type, _ in recorder.records]
    assert types == ["memory_tool_result"]
    assert recorder.records[0][1]["status"] == "budget_exceeded"
    assert recorder.records[0][1]["error_code"] == "MEMORY_TOOL_BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_no_pending_calls_is_a_noop() -> None:
    gateway = FakeMemoryToolsGateway()
    runtime = _runtime(memory_gateway=gateway)
    result = await run_memory_tools(_state(), runtime=runtime)

    assert gateway.calls == []
    assert result["memory_tool_outputs"] == []
    assert "memory_tool_records" not in result


@pytest.mark.asyncio
async def test_read_hint_points_at_the_first_line_the_model_did_not_see() -> None:
    """回归（review I-4）：截断提示必须指向**已投递**的下一行，不能跳过未投递的行。

    曾经的实现用完整 content 的行数算 hint：200 行 read 因字符上限只投递约 70 行，
    提示却写 `line_offset=200` → 71–200 行**永远不会被读到**且没有任何错误。
    """
    gateway = FakeMemoryToolsGateway()
    # 每行足够长，保证 200 行一定超过 RESULT_MAX_CHARS（否则不触发截断）
    lines = [f"第 {index} 行正文" + "细节" * 60 for index in range(200)]
    gateway.read_results = [_read_payload(content="\n".join(lines), line_offset=0, total_lines=200)]
    runtime = _runtime(memory_gateway=gateway)

    result = await run_memory_tools(
        _state(
            memory_pending_tool_calls=[
                _call("memory.read", {"memory_id": "mastery:椭圆", "max_lines": 200})
            ]
        ),
        runtime=runtime,
    )

    output = result["memory_tool_outputs"][0]["output"]
    assert result["memory_truncated"] is True
    payload = json.loads(output)
    hint = payload["hint"]
    delivered = str(payload["content"]).splitlines()
    # 提示必须等于"服务端起始行 + 真正投递的行数"
    assert hint["line_offset"] == len(delivered)
    assert 0 < len(delivered) < 200, "字符上限应当真的裁掉了后面的行"
    # 未投递的第一行必须正好是 hint 指向的那一行（不漏不跳）
    assert lines[hint["line_offset"]] not in delivered
    assert len(delivered) == hint["line_offset"]
