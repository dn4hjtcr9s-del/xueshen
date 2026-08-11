"""图谱状态转换与证据评估单元测试（§16.2 / §16.3 / §9.3 / §23.2）。"""

from datetime import UTC, datetime, timedelta

from backend.memory.contracts.evidence import GraphProjectionEvidence
from backend.memory.services.graph_state_service import (
    USER_TRANSITIONS,
    evaluate_evidence,
)
from backend.settings import Settings

NOW = datetime(2026, 8, 10, 8, 0, 0, tzinfo=UTC)
S = Settings(app_env="test")


def ev(ref: str, direction: str, strength: float, days_ago: float) -> GraphProjectionEvidence:
    return GraphProjectionEvidence.model_validate(
        {
            "evidence_ref": ref,
            "direction": direction,
            "strength": strength,
            "occurred_at": (NOW - timedelta(days=days_ago)).isoformat(),
        }
    )


def test_user_transition_table() -> None:
    # 四状态 × 三动作全覆盖（§16.2）
    assert USER_TRANSITIONS[(None, "mark_unfamiliar")] == "learning"
    assert USER_TRANSITIONS[(None, "mark_familiar")] == "proficient"
    assert USER_TRANSITIONS[(None, "clear")] is None
    assert USER_TRANSITIONS[("learning", "mark_familiar")] == "proficient"
    assert USER_TRANSITIONS[("proficient", "mark_unfamiliar")] == "learning"
    assert USER_TRANSITIONS[("expert", "mark_familiar")] == "proficient"
    assert USER_TRANSITIONS[("expert", "clear")] is None
    # expert 不在用户动作枚举中
    assert all(action != "mark_expert" for _, action in USER_TRANSITIONS)


def test_no_evidence_no_status() -> None:
    result = evaluate_evidence([], settings=S, now=NOW)
    assert result.status is None


def test_single_learning_evidence_gives_learning() -> None:
    result = evaluate_evidence([ev("r1", "learning", 0.6, 3)], settings=S, now=NOW)
    assert result.status == "learning"


def test_single_positive_not_enough_for_proficient() -> None:
    result = evaluate_evidence([ev("r1", "positive", 0.8, 3)], settings=S, now=NOW)
    assert result.status == "learning"  # 熟练至少需要两条独立正向证据


def test_two_positives_give_proficient() -> None:
    result = evaluate_evidence(
        [ev("r1", "positive", 0.8, 3), ev("r2", "positive", 0.75, 10)],
        settings=S,
        now=NOW,
    )
    assert result.status == "proficient"


def test_weak_positive_not_counted() -> None:
    result = evaluate_evidence(
        [ev("r1", "positive", 0.5, 3), ev("r2", "positive", 0.6, 10)],
        settings=S,
        now=NOW,
    )
    assert result.status is None  # 裁决 1A：低于 GRAPH_POSITIVE_STRENGTH 不计入任何状态


def test_expert_requires_three_strong_span_and_self_demo() -> None:
    # 跨度不足 14 天：最多 proficient
    result = evaluate_evidence(
        [
            ev("user_solution:a", "strong_positive", 0.9, 1),
            ev("r2", "strong_positive", 0.9, 3),
            ev("r3", "strong_positive", 0.9, 5),
        ],
        settings=S,
        now=NOW,
    )
    assert result.status == "proficient"
    # 跨度 >=14 天且含自主解答证据：expert
    result = evaluate_evidence(
        [
            ev("user_solution:a", "strong_positive", 0.9, 1),
            ev("r2", "strong_positive", 0.9, 10),
            ev("r3", "strong_positive", 0.9, 20),
        ],
        settings=S,
        now=NOW,
    )
    assert result.status == "expert"


def test_expert_blocked_without_self_demonstration() -> None:
    result = evaluate_evidence(
        [
            ev("r1", "strong_positive", 0.9, 1),
            ev("r2", "strong_positive", 0.9, 10),
            ev("r3", "strong_positive", 0.9, 20),
        ],
        settings=S,
        now=NOW,
    )
    assert result.status == "proficient"


def test_expert_blocked_by_unresolved_conflict() -> None:
    result = evaluate_evidence(
        [
            ev("user_solution:a", "strong_positive", 0.9, 1),
            ev("r2", "strong_positive", 0.9, 10),
            ev("r3", "strong_positive", 0.9, 20),
            ev("c1", "conflict", 0.9, 2),
        ],
        settings=S,
        now=NOW,
    )
    # 裁决 2A：单条强冲突只阻止 expert 晋升，不降级 → 回落 proficient
    assert result.status == "proficient"


def test_two_strong_conflicts_downgrade() -> None:
    result = evaluate_evidence(
        [ev("c1", "conflict", 0.9, 2), ev("c2", "conflict", 0.86, 5)],
        settings=S,
        now=NOW,
    )
    assert result.status == "learning"
    assert "REVIEW_AFTER_CONFLICT" in result.reason_codes


def test_single_normal_error_no_downgrade() -> None:
    result = evaluate_evidence(
        [ev("r1", "positive", 0.8, 3), ev("c1", "conflict", 0.5, 2)],
        settings=S,
        now=NOW,
    )
    assert result.status == "learning"  # 一次普通错误不降级，但单条正向也只能 learning


def test_evidence_dedup_and_window() -> None:
    result = evaluate_evidence(
        [
            ev("r1", "positive", 0.8, 3),
            ev("r1", "positive", 0.8, 4),  # 重复 ref 只计一次
            ev("r2", "positive", 0.8, 200),  # 超出 180 天窗口
        ],
        settings=S,
        now=NOW,
    )
    assert result.status == "learning"
