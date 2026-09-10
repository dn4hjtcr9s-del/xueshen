"""Evidence 批次契约测试（memory-rebuild §2.6 / §5.2 Phase 0-A）。

覆盖批次上限、成员状态迁移、游标编解码，以及新增状态/类型与 settings 默认值的
一致性——后者专门防止 contracts 与 settings 两侧默认值漂移。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from backend.memory.contracts.batch import (
    BATCH_IDEMPOTENCY_KEY_TEMPLATE,
    BATCH_OPERATION_TYPE,
    EVIDENCE_BATCH_MAX_DEFAULT,
    EVIDENCE_MIN_AGE_HOURS_DEFAULT,
    SUMMARY_DAILY_TIME_DEFAULT,
    SUMMARY_LLM_CONCURRENCY_DEFAULT,
    SUMMARY_MAX_USERS_PER_RUN_DEFAULT,
    BatchContractError,
    BatchCursor,
    EvidenceBatchLimits,
    EvidenceBatchMember,
    EvidenceBatchPlan,
    validate_member_transition,
)
from backend.memory.contracts.common import (
    OPERATION_ROUTING,
    PRIORITY_P2,
    TERMINAL_STATUSES,
    OperationStatus,
    OperationType,
    max_attempts_for_priority,
)

_NOW = datetime(2026, 8, 28, 0, 0, 0, tzinfo=UTC)
_USER_ID = uuid.UUID("44444444-4444-4444-8444-444444444444")


# ---------------------------------------------------------------------------
# 默认值与 settings 一致性（防漂移）
# ---------------------------------------------------------------------------


def test_contract_defaults_match_settings_defaults() -> None:
    from backend.settings import Settings

    settings = Settings()
    assert settings.memory_summary_batch_max_evidence == EVIDENCE_BATCH_MAX_DEFAULT
    assert settings.memory_evidence_min_age_hours == EVIDENCE_MIN_AGE_HOURS_DEFAULT
    assert settings.memory_summary_max_users_per_run == SUMMARY_MAX_USERS_PER_RUN_DEFAULT
    assert settings.memory_summary_llm_concurrency == SUMMARY_LLM_CONCURRENCY_DEFAULT
    assert settings.memory_summary_daily_time.strftime("%H:%M") == SUMMARY_DAILY_TIME_DEFAULT


def test_batch_operation_type_is_a_registered_operation_type() -> None:
    from typing import get_args

    assert BATCH_OPERATION_TYPE == "summarize_user_memory_batch"
    assert BATCH_OPERATION_TYPE in get_args(OperationType)
    assert BATCH_OPERATION_TYPE in OPERATION_ROUTING


def test_batch_routing_is_evidence_p2() -> None:
    """Phase 0 决策：批量总结与 conversation_evidence 同为 evidence + P2。"""
    assert OPERATION_ROUTING[BATCH_OPERATION_TYPE] == ("evidence", PRIORITY_P2)
    assert max_attempts_for_priority(PRIORITY_P2) == 4


def test_batch_idempotency_key_template() -> None:
    assert BATCH_IDEMPOTENCY_KEY_TEMPLATE.format(user_id="u1", date="2026-08-28") == (
        "summarize:u1:2026-08-28"
    )


# ---------------------------------------------------------------------------
# pending_batch 状态语义
# ---------------------------------------------------------------------------


def test_pending_batch_is_a_known_status() -> None:
    from typing import get_args

    assert "pending_batch" in get_args(OperationStatus)


def test_pending_batch_is_not_terminal() -> None:
    """非终态：批量入批前一直等待，不能被当作已完成清理掉。"""
    assert "pending_batch" not in TERMINAL_STATUSES


# ---------------------------------------------------------------------------
# 上限与成员
# ---------------------------------------------------------------------------


def test_limits_defaults() -> None:
    limits = EvidenceBatchLimits()
    assert limits.max_evidence == 50
    assert limits.max_users_per_run == 50
    assert limits.llm_concurrency == 8


def test_limits_reject_zero_or_absurd_values() -> None:
    with pytest.raises(ValidationError):
        EvidenceBatchLimits(max_evidence=0)
    with pytest.raises(ValidationError):
        EvidenceBatchLimits(llm_concurrency=0)
    with pytest.raises(ValidationError):
        EvidenceBatchLimits(max_evidence=10_000)


def test_plan_over_limit_is_rejected() -> None:
    """§4.5-① 决议：单批默认上限 50 条。"""
    limits = EvidenceBatchLimits(max_evidence=3)
    with pytest.raises(ValidationError):
        EvidenceBatchPlan(
            batch_operation_id=uuid.uuid4(),
            user_id=_USER_ID,
            member_operation_ids=[uuid.uuid4() for _ in range(4)],
            limits=limits,
            created_at=_NOW,
        )


def test_plan_at_limit_is_accepted() -> None:
    limits = EvidenceBatchLimits(max_evidence=3)
    plan = EvidenceBatchPlan(
        batch_operation_id=uuid.uuid4(),
        user_id=_USER_ID,
        member_operation_ids=[uuid.uuid4() for _ in range(3)],
        limits=limits,
        created_at=_NOW,
    )
    assert len(plan.member_operation_ids) == 3


def test_plan_rejects_duplicate_members() -> None:
    """一个证据只进一个批（§2.6「单 FK 足够」）。"""
    member = uuid.uuid4()
    with pytest.raises(ValidationError):
        EvidenceBatchPlan(
            batch_operation_id=uuid.uuid4(),
            user_id=_USER_ID,
            member_operation_ids=[member, member],
            created_at=_NOW,
        )


def test_plan_is_member_matches_only_its_own_members() -> None:
    members = [uuid.uuid4() for _ in range(2)]
    plan = EvidenceBatchPlan(
        batch_operation_id=uuid.uuid4(),
        user_id=_USER_ID,
        member_operation_ids=members,
        created_at=_NOW,
    )
    assert plan.is_member(members[0])
    assert not plan.is_member(uuid.uuid4())


def test_member_defaults_to_pending_batch() -> None:
    member = EvidenceBatchMember(
        operation_id=uuid.uuid4(),
        user_id=_USER_ID,
        eligible_at=_NOW + timedelta(hours=6),
        created_at=_NOW,
    )
    assert member.status == "pending_batch"


def test_member_rejects_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        EvidenceBatchMember(
            operation_id=uuid.uuid4(),
            user_id=_USER_ID,
            eligible_at=datetime(2026, 8, 28, 0, 0, 0),
            created_at=_NOW,
        )


# ---------------------------------------------------------------------------
# 成员状态迁移（§2.6 状态机第 3/4 步）
# ---------------------------------------------------------------------------


def test_legal_member_transitions() -> None:
    assert validate_member_transition("pending_batch", "succeeded") == "succeeded"
    assert validate_member_transition("pending_batch", "dead_letter") == "dead_letter"
    assert validate_member_transition("pending_batch", "pending_batch") == "pending_batch"


def test_terminal_members_cannot_be_reopened() -> None:
    with pytest.raises(BatchContractError):
        validate_member_transition("succeeded", "pending_batch")
    with pytest.raises(BatchContractError):
        validate_member_transition("dead_letter", "succeeded")


def test_unknown_member_status_is_rejected() -> None:
    with pytest.raises(BatchContractError):
        validate_member_transition("queued", "succeeded")


# ---------------------------------------------------------------------------
# 游标
# ---------------------------------------------------------------------------


def _cursor() -> BatchCursor:
    return BatchCursor(
        eligible_at=_NOW,
        created_at=_NOW + timedelta(seconds=1),
        operation_id=uuid.uuid4(),
    )


def test_cursor_round_trip() -> None:
    cursor = _cursor()
    assert BatchCursor.decode(cursor.encode()) == cursor


def test_cursor_encoding_is_deterministic() -> None:
    cursor = _cursor()
    assert cursor.encode() == cursor.encode()


def test_cursor_fits_maintenance_runs_column() -> None:
    """载体是 memory_maintenance_runs.cursor varchar(500)。"""
    assert len(_cursor().encode()) <= 500


def test_corrupt_cursor_is_rejected() -> None:
    with pytest.raises(BatchContractError):
        BatchCursor.decode("not json")
    with pytest.raises(BatchContractError):
        BatchCursor.decode("[1,2,3]")
    with pytest.raises(BatchContractError):
        BatchCursor.decode('{"eligible_at": "2026-08-28T00:00:00+00:00"}')


def test_cursor_orders_stably_by_three_keys() -> None:
    """稳定排序 (eligible_at, created_at, operation_id)：避免重试时成员漂移。"""
    operation_id = uuid.UUID("55555555-5555-4555-8555-555555555555")
    first = BatchCursor(eligible_at=_NOW, created_at=_NOW, operation_id=operation_id)
    second = BatchCursor(
        eligible_at=_NOW,
        created_at=_NOW + timedelta(microseconds=1),
        operation_id=operation_id,
    )
    assert first.encode() != second.encode()
