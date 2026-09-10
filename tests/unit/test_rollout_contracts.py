"""Rollout JSONL 契约测试（memory-rebuild §5.2 Phase 0-A / §5.11）。

覆盖固定样例与拒绝样例：类型白名单、ordinal 下界、时间戳格式与 UTC 要求、
thread 级 / turn 级记录的 turn_id 归属、payload 必填字段、指针字节范围。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.conversation.contracts.rollout import (
    MEMORY_TOOL_CALL_BUDGET,
    PAYLOAD_MODELS,
    ROLLOUT_RECORD_TYPES,
    ROLLOUT_SCHEMA_VERSION,
    MemoryToolCallPayload,
    RolloutContractError,
    RolloutPointer,
    RolloutRecord,
    format_recorded_at,
    parse_rollout_line,
    serialize_rollout_line,
    validate_rollout_payload,
)

_THREAD_ID = uuid.UUID("11111111-1111-4111-8111-111111111111")
_USER_ID = uuid.UUID("22222222-2222-4222-8222-222222222222")
_TURN_ID = uuid.UUID("33333333-3333-4333-8333-333333333333")
_NOW = datetime(2026, 8, 28, 5, 41, 7, 123_000, tzinfo=UTC)


def _user_message_record(ordinal: int = 42) -> RolloutRecord:
    return RolloutRecord(
        recorded_at=_NOW,
        ordinal=ordinal,
        type="user_message",
        turn_id=_TURN_ID,
        payload={
            "message_id": str(uuid.uuid4()),
            "sequence": 1,
            "role": "user",
            "content": "椭圆的第一定义是什么？",
            "content_hash": "a" * 64,
            "occurred_at": _NOW.isoformat(),
        },
    )


# ---------------------------------------------------------------------------
# 固定样例
# ---------------------------------------------------------------------------


def test_line_format_is_compact_and_carries_ms_utc_timestamp() -> None:
    """行格式对齐 §1.5 样例：紧凑 JSON、recorded_at 为 UTC 毫秒 ``...sssZ``。"""
    line = serialize_rollout_line(_user_message_record())

    assert "\n" not in line
    assert '": "' not in line  # 紧凑分隔符
    raw = json.loads(line)
    assert raw["ordinal"] == 42
    assert raw["type"] == "user_message"
    assert raw["turn_id"] == str(_TURN_ID)
    assert raw["recorded_at"] == "2026-08-28T05:41:07.123Z"


def test_line_round_trip_preserves_all_fields() -> None:
    original = _user_message_record()
    restored = parse_rollout_line(serialize_rollout_line(original))

    assert restored.type == original.type
    assert restored.ordinal == original.ordinal
    assert restored.turn_id == original.turn_id
    assert restored.recorded_at == original.recorded_at
    assert restored.payload["content"] == "椭圆的第一定义是什么？"


def test_cjk_content_is_not_escaped() -> None:
    """正文是中文，落盘应是 UTF-8 原文而非 \\uXXXX 转义（省体积、jq 可读）。"""
    line = serialize_rollout_line(_user_message_record())
    assert "椭圆的第一定义是什么？" in line


def test_thread_meta_sample_is_accepted_without_turn_id() -> None:
    record = RolloutRecord(
        recorded_at=_NOW,
        ordinal=0,
        type="thread_meta",
        payload={
            "thread_id": str(_THREAD_ID),
            "user_id": str(_USER_ID),
            "created_at": _NOW.isoformat(),
            "graph_version": "conv-graph-v1",
            "schema_version": ROLLOUT_SCHEMA_VERSION,
        },
    )
    assert record.turn_id is None
    assert record.payload["schema_version"] == ROLLOUT_SCHEMA_VERSION


def test_every_whitelisted_type_has_a_payload_model() -> None:
    """类型集合与 payload 模型必须一一对应，否则白名单会出现"能落不能校验"的洞。"""
    assert set(PAYLOAD_MODELS) == set(ROLLOUT_RECORD_TYPES)
    assert len(ROLLOUT_RECORD_TYPES) == 12


def test_phase5_tool_types_are_reserved_now() -> None:
    """Phase 0 一次定全类型集合（含 Phase 5 才写入的工具记录）。"""
    for record_type in ("memory_prime", "memory_tool_call", "memory_tool_result"):
        assert record_type in ROLLOUT_RECORD_TYPES


# ---------------------------------------------------------------------------
# 拒绝样例
# ---------------------------------------------------------------------------


def test_unknown_record_type_is_rejected() -> None:
    with pytest.raises(RolloutContractError):
        validate_rollout_payload("drop_table", {})


def test_unknown_record_type_in_line_is_rejected() -> None:
    line = json.dumps(
        {
            "recorded_at": "2026-08-28T05:41:07.123Z",
            "ordinal": 1,
            "type": "gateway_http_detail",
            "turn_id": str(_TURN_ID),
            "payload": {},
        }
    )
    with pytest.raises(RolloutContractError):
        parse_rollout_line(line)


def test_negative_ordinal_is_rejected() -> None:
    with pytest.raises(RolloutContractError):
        parse_rollout_line(
            json.dumps(
                {
                    "recorded_at": "2026-08-28T05:41:07.123Z",
                    "ordinal": -1,
                    "type": "turn_started",
                    "turn_id": str(_TURN_ID),
                    "payload": {
                        "turn_id": str(_TURN_ID),
                        "request_id": "req-1",
                        "started_at": _NOW.isoformat(),
                    },
                }
            )
        )


def test_naive_timestamp_is_rejected() -> None:
    """无时区的时间戳一律拒绝：两个时间戳语义（文件名 / 落盘）都要求 UTC。"""
    with pytest.raises(ValidationError):
        RolloutRecord(
            recorded_at=datetime(2026, 8, 28, 5, 41, 7),
            ordinal=1,
            type="turn_started",
            turn_id=_TURN_ID,
            payload={
                "turn_id": str(_TURN_ID),
                "request_id": "req-1",
                "started_at": _NOW.isoformat(),
            },
        )


def test_non_utc_offset_is_normalized_to_utc() -> None:
    """带偏移的时间戳允许，但落盘必须归一为 UTC。"""
    tz = timezone(timedelta(hours=8))
    record = RolloutRecord(
        recorded_at=datetime(2026, 8, 28, 13, 41, 7, 123_000, tzinfo=tz),
        ordinal=1,
        type="turn_started",
        turn_id=_TURN_ID,
        payload={
            "turn_id": str(_TURN_ID),
            "request_id": "req-1",
            "started_at": _NOW.isoformat(),
        },
    )
    assert json.loads(serialize_rollout_line(record))["recorded_at"] == "2026-08-28T05:41:07.123Z"


def test_turn_scoped_type_requires_turn_id() -> None:
    with pytest.raises(ValidationError):
        RolloutRecord(recorded_at=_NOW, ordinal=1, type="turn_started", payload={})


def test_thread_meta_must_not_carry_turn_id() -> None:
    with pytest.raises(ValidationError):
        RolloutRecord(
            recorded_at=_NOW,
            ordinal=0,
            type="thread_meta",
            turn_id=_TURN_ID,
            payload={
                "thread_id": str(_THREAD_ID),
                "user_id": str(_USER_ID),
                "created_at": _NOW.isoformat(),
                "graph_version": "v1",
            },
        )


def test_missing_required_payload_field_is_rejected() -> None:
    """turn_completed 必须带 status 与 degraded_flags（§5.3 写入顺序第 6 条）。"""
    with pytest.raises(RolloutContractError):
        validate_rollout_payload("turn_completed", {"turn_id": str(_TURN_ID)})


def test_unknown_payload_field_is_rejected() -> None:
    with pytest.raises(RolloutContractError):
        validate_rollout_payload(
            "embedded_queries",
            {"model": "text-embedding-v4", "dimensions": 1024, "vectors": [[0.1]]},
        )


def test_empty_and_malformed_lines_are_rejected() -> None:
    with pytest.raises(RolloutContractError):
        parse_rollout_line("   ")
    with pytest.raises(RolloutContractError):
        parse_rollout_line("{not json")
    with pytest.raises(RolloutContractError):
        parse_rollout_line("[1, 2, 3]")


def test_backslash_n_in_line_is_rejected_as_two_records() -> None:
    """一行一条记录：内嵌换行意味着上游拼错了行。"""
    with pytest.raises(RolloutContractError):
        parse_rollout_line('{"ordinal": 1}\n{"ordinal": 2}')


# ---------------------------------------------------------------------------
# 工具调用预算（§2.7-③ 决议 B 组：独立预算，默认 6 次/轮）
# ---------------------------------------------------------------------------


def test_tool_call_budget_default_is_six() -> None:
    assert MEMORY_TOOL_CALL_BUDGET == 6


def test_tool_call_index_within_budget_is_accepted() -> None:
    payload = MemoryToolCallPayload(
        call_id="call-1",
        tool="memory.search",
        call_index=MEMORY_TOOL_CALL_BUDGET,
        arguments={"queries": ["椭圆"]},
    )
    assert payload.call_index == MEMORY_TOOL_CALL_BUDGET


def test_tool_call_index_over_budget_is_rejected() -> None:
    with pytest.raises(ValidationError):
        MemoryToolCallPayload(
            call_id="call-7",
            tool="memory.search",
            call_index=MEMORY_TOOL_CALL_BUDGET + 1,
        )


# ---------------------------------------------------------------------------
# 指针
# ---------------------------------------------------------------------------


def test_pointer_accepts_forward_range() -> None:
    pointer = RolloutPointer(
        segment_id=uuid.uuid4(),
        ordinal=42,
        byte_offset_start=0,
        byte_offset_end=512,
    )
    assert pointer.byte_offset_end - pointer.byte_offset_start == 512


def test_pointer_rejects_empty_or_inverted_range() -> None:
    segment_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        RolloutPointer(segment_id=segment_id, ordinal=0, byte_offset_start=10, byte_offset_end=10)
    with pytest.raises(ValidationError):
        RolloutPointer(segment_id=segment_id, ordinal=0, byte_offset_start=10, byte_offset_end=9)


def test_format_recorded_at_zero_pads_milliseconds() -> None:
    value = datetime(2026, 1, 2, 3, 4, 5, 7_000, tzinfo=UTC)
    assert format_recorded_at(value) == "2026-01-02T03:04:05.007Z"
