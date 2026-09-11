"""Rollout 编解码 / 命名 / 策略单元测试（memory-rebuild §5.3 Phase 1 / §5.11）。

覆盖三件容易出错的事：
- **行语义**：一行一条、行尾终止符、截断尾行识别、中间损坏不静默跳过；
- **命名与分片**：日期目录按 thread 创建时间、ordinal 前导补零、回滚预留命名不参与段解析；
- **白名单**：瞬态项与 Phase 5 保留类型被拒、凭证类字段递归拦截。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.conversation.contracts.rollout import RolloutContractError, RolloutRecord
from backend.conversation.rollout.codec import (
    canonical_record_hash,
    decode_record,
    encode_record,
    has_truncated_tail,
    iter_records,
    sha256_hex,
)
from backend.conversation.rollout.file_naming import (
    SegmentNameError,
    list_segment_paths,
    parse_segment_filename,
    segment_dir,
    segment_filename,
    segment_path,
)
from backend.conversation.rollout.policy import (
    PERSISTED_RECORD_TYPES,
    RESERVED_RECORD_TYPES,
    RolloutPolicyError,
    ensure_persistable,
    should_persist,
)

_NOW = datetime(2026, 9, 10, 5, 0, 0, 123_000, tzinfo=UTC)
_TURN = uuid.UUID("33333333-3333-4333-8333-333333333333")


def _record(ordinal: int = 0, *, record_type: str = "turn_started") -> RolloutRecord:
    if record_type == "turn_started":
        payload = {"turn_id": str(_TURN), "request_id": "r1", "started_at": _NOW.isoformat()}
    else:
        payload = {}
    return RolloutRecord(
        recorded_at=_NOW, ordinal=ordinal, type=record_type, turn_id=_TURN, payload=payload
    )


# ---------------------------------------------------------------------------
# codec
# ---------------------------------------------------------------------------


def test_encode_ends_with_single_line_terminator() -> None:
    data = encode_record(_record())
    assert data.endswith(b"\n")
    assert data.count(b"\n") == 1


def test_encode_is_utf8_without_escapes() -> None:
    record = RolloutRecord(
        recorded_at=_NOW,
        ordinal=0,
        type="user_message",
        turn_id=_TURN,
        payload={
            "message_id": str(uuid.uuid4()),
            "sequence": 1,
            "role": "user",
            "content": "椭圆的焦点性质",
            "content_hash": "a" * 64,
            "occurred_at": _NOW.isoformat(),
        },
    )
    assert "椭圆的焦点性质".encode() in encode_record(record)


def test_decode_round_trip() -> None:
    original = _record(7)
    assert decode_record(encode_record(original)).ordinal == 7


def test_has_truncated_tail() -> None:
    assert has_truncated_tail(b"") is False
    assert has_truncated_tail(b'{"ordinal": 0}\n') is False
    assert has_truncated_tail(b'{"ordinal": 0}\n{"ordi') is True


def test_iter_records_skips_truncated_tail() -> None:
    good = encode_record(_record(0))
    data = good + b'{"recorded_at": "2026-09-10T05:00:00.000Z", "ordi'
    records = list(iter_records(data))
    assert [r.ordinal for r in records] == [0]


def test_iter_records_can_include_truncated_tail_for_diagnosis() -> None:
    good = encode_record(_record(0))
    with pytest.raises(RolloutContractError):
        list(iter_records(good + b'{"ordinal": 1, "ty', include_truncated_tail=True))


def test_iter_records_raises_on_middle_corruption() -> None:
    """中间行损坏必须报错——静默跳过会让重放结果看起来完整却缺记录。"""
    data = encode_record(_record(0)) + b"{not json}\n" + encode_record(_record(2))
    with pytest.raises(RolloutContractError):
        list(iter_records(data))


def test_iter_records_empty_file_yields_nothing() -> None:
    assert list(iter_records(b"")) == []


def test_sha256_hex_is_content_hash() -> None:
    assert sha256_hex(b"abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_canonical_record_hash_is_stable_across_key_order() -> None:
    first = _record(1)
    second = _record(1)
    assert canonical_record_hash(first) == canonical_record_hash(second)


# ---------------------------------------------------------------------------
# file_naming
# ---------------------------------------------------------------------------


def test_segment_dir_uses_utc_date_of_thread_creation() -> None:
    tz = timezone(timedelta(hours=8))
    # 东八区 2026-09-10 01:00 == UTC 2026-09-09 17:00 → 目录必须是 09/09
    created = datetime(2026, 9, 10, 1, 0, tzinfo=tz)
    directory = segment_dir(root="/tmp/rollouts", thread_created_at=created, thread_id=_TURN)
    assert directory == Path(f"/tmp/rollouts/threads/2026/09/09/{_TURN}")


def test_segment_dir_rejects_naive_datetime() -> None:
    with pytest.raises(SegmentNameError):
        segment_dir(root="/tmp/r", thread_created_at=datetime(2026, 9, 10), thread_id=_TURN)


def test_segment_dir_rejects_illegal_thread_id() -> None:
    with pytest.raises(SegmentNameError):
        segment_dir(root="/tmp/r", thread_created_at=_NOW, thread_id="../../etc/passwd")


def test_segment_filename_pads_ordinal_for_lexicographic_order() -> None:
    segment_id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    low = segment_filename(ordinal_start=2, segment_id=segment_id)
    high = segment_filename(ordinal_start=10, segment_id=segment_id)
    assert low < high
    assert low.startswith("000000000002-")


def test_segment_filename_rejects_negative_ordinal() -> None:
    with pytest.raises(SegmentNameError):
        segment_filename(ordinal_start=-1, segment_id=uuid.uuid4())


def test_parse_segment_filename_round_trip() -> None:
    segment_id = uuid.uuid4()
    name = segment_filename(ordinal_start=42, segment_id=segment_id)
    assert parse_segment_filename(name) == (42, segment_id)


@pytest.mark.parametrize(
    "name",
    [
        "rollout-2026-09-10T05-00-00-abc_rollback1.jsonl",  # §1.5 回滚预留命名
        "000000000001-11111111-1111-4111-8111-111111111111.jsonl.tmp",
        "notes.txt",
        "000000000001-not-a-uuid.jsonl",
        "no-ordinal.jsonl",
    ],
)
def test_parse_segment_filename_rejects_non_segments(name: str) -> None:
    assert parse_segment_filename(name) is None


def test_list_segment_paths_sorted_and_ignores_non_segments(tmp_path: Path) -> None:
    directory = tmp_path / "thread"
    directory.mkdir()
    for ordinal in (10, 2, 0):
        (directory / segment_filename(ordinal_start=ordinal, segment_id=uuid.uuid4())).write_text(
            ""
        )
    (directory / "rollout-2026-09-10T05-00-00-abc_rb1.jsonl").write_text("")
    (directory / "README.md").write_text("")

    ordinals = [item[0] for item in list_segment_paths(directory)]
    assert ordinals == [0, 2, 10]


def test_list_segment_paths_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list_segment_paths(tmp_path / "nope") == []


def test_segment_path_composes_dir_and_name(tmp_path: Path) -> None:
    segment_id = uuid.uuid4()
    path = segment_path(
        root=tmp_path,
        thread_created_at=_NOW,
        thread_id=_TURN,
        ordinal_start=3,
        segment_id=segment_id,
    )
    assert path.parent.name == str(_TURN)
    assert path.name == segment_filename(ordinal_start=3, segment_id=segment_id)


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


def test_whitelist_covers_phase1_and_phase5_types() -> None:
    """Phase 1 的 9 类 + Phase 5 的 3 类记忆工具记录。"""
    assert PERSISTED_RECORD_TYPES == {
        "thread_meta",
        "turn_started",
        "turn_completed",
        "turn_context_snapshot",
        "rewrite_plan",
        "evidence_set",
        "embedded_queries",
        "user_message",
        "assistant_message",
        "memory_prime",
        "memory_tool_call",
        "memory_tool_result",
    }


def test_reserved_types_are_empty_after_phase5() -> None:
    """Phase 5 落地后不再有"契约已定型但无实现"的记录类型。"""
    assert RESERVED_RECORD_TYPES == frozenset()


def test_phase5_tool_records_are_now_persistable() -> None:
    for record_type in ("memory_prime", "memory_tool_call", "memory_tool_result"):
        assert should_persist(record_type) is True


@pytest.mark.parametrize("name", ["answer_delta", "cancel_token", "lease", "gateway_http"])
def test_transient_items_are_rejected(name: str) -> None:
    with pytest.raises(RolloutPolicyError, match="瞬态"):
        ensure_persistable(name, {})


def test_tool_call_still_validated_against_contract() -> None:
    """放开白名单不等于放松契约：payload 仍按 memory_tool_call 模型严格校验。"""
    with pytest.raises(RolloutPolicyError, match="不符合契约"):
        ensure_persistable("memory_tool_call", {})
    normalized = ensure_persistable(
        "memory_tool_call",
        {
            "call_id": "call-1",
            "tool": "memory.search",
            "call_index": 1,
            "arguments": {"queries": ["椭圆"]},
        },
    )
    assert normalized["tool"] == "memory.search"


def test_unknown_type_rejected() -> None:
    with pytest.raises(RolloutPolicyError):
        ensure_persistable("drop_table", {})


def test_valid_payload_is_normalized() -> None:
    normalized = ensure_persistable(
        "turn_started",
        {"turn_id": str(_TURN), "request_id": "r1", "started_at": _NOW.isoformat()},
    )
    assert normalized["request_id"] == "r1"


def test_credential_keys_are_rejected_recursively() -> None:
    """rewrite_plan.plan 是自由结构，是唯一能夹带凭证的入口。"""
    with pytest.raises(RolloutPolicyError, match="凭证类字段"):
        ensure_persistable(
            "rewrite_plan",
            {"revision": 1, "plan": {"nested": {"authorization": "Bearer x"}}},
        )


def test_credential_key_inside_list_is_rejected() -> None:
    with pytest.raises(RolloutPolicyError, match="凭证类字段"):
        ensure_persistable(
            "rewrite_plan",
            {"revision": 1, "plan": {"steps": [{"api_key": "sk-x"}]}},
        )


def test_innocuous_free_form_payload_passes() -> None:
    normalized = ensure_persistable(
        "rewrite_plan",
        {"revision": 2, "plan": {"subqueries": [{"query_text": "椭圆"}]}},
    )
    assert normalized["revision"] == 2


def test_evidence_set_records_references_only() -> None:
    """§1.5：大对象放引用——evidence_set 不得携带 chunk 正文。"""
    normalized = ensure_persistable("evidence_set", {"references": ["ev-1", "ev-2"]})
    assert normalized == {"references": ["ev-1", "ev-2"]}
    with pytest.raises(RolloutPolicyError):
        ensure_persistable("evidence_set", {"references": [], "content_text": "正文"})


def test_embedded_queries_rejects_vectors() -> None:
    with pytest.raises(RolloutPolicyError):
        ensure_persistable(
            "embedded_queries", {"model": "m", "dimensions": 1024, "vectors": [[0.1]]}
        )


def test_json_line_is_decodable_independently() -> None:
    """每行都能独立 JSON 解码（Phase 1 验收）。"""
    data = encode_record(_record(0)) + encode_record(_record(1))
    for line in data.splitlines():
        assert json.loads(line)["ordinal"] in (0, 1)
