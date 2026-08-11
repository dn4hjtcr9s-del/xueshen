"""图谱推荐分级排序单元测试（§16.5 / §23.1）。

覆盖：六个优先级桶的分派、expert 默认不推荐（强冲突证据例外）、
同桶内确定性排序、PREREQUISITE_GAP/NEXT_GRAPH_NODE 的边方向语义。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from backend.memory.services.recommendation_service import (
    RECENT_ACTIVITY_DAYS,
    STALE_PROFICIENCY_DAYS,
    has_strong_conflict,
    rank_recommendations,
)

NOW = datetime(2026, 8, 11, tzinfo=UTC)
STRONG = 0.8


def _overlay(
    status: str,
    *,
    updated_at: datetime | None = NOW,
    evidence: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "node_id": "n000",
        "status": status,
        "updated_at": updated_at,
        "evidence_snapshot": evidence or [],
    }


def _activity(
    *,
    last_viewed_at: datetime | None = None,
    last_bookmarked_at: datetime | None = None,
    last_check_in_at: datetime | None = None,
    event_count: int = 0,
) -> dict[str, object]:
    return {
        "last_viewed_at": last_viewed_at,
        "last_bookmarked_at": last_bookmarked_at,
        "last_check_in_at": last_check_in_at,
        "event_count": event_count,
        "updated_at": last_viewed_at,
    }


def _rank(**overrides: object):
    base: dict[str, object] = {
        "node_ids": [],
        "edges": [],
        "overlays": {},
        "activities": {},
        "review_signal_node_ids": set(),
        "now": NOW,
        "strong_conflict_threshold": STRONG,
    }
    base.update(overrides)
    return rank_recommendations(**base)  # type: ignore[arg-type]


def _by_node(ranked: list[tuple[str, int, list[str], tuple]]) -> dict[str, tuple[int, list[str]]]:
    return {node_id: (bucket, codes) for node_id, bucket, codes, _key in ranked}


# ---------------------------------------------------------------------------
# 各优先级桶分派
# ---------------------------------------------------------------------------


def test_bucket1_learning_with_recent_activity() -> None:
    ranked = _rank(
        node_ids=["n001"],
        overlays={"n001": _overlay("learning")},
        activities={"n001": _activity(last_viewed_at=NOW - timedelta(days=3))},
    )
    assert _by_node(ranked)["n001"] == (1, ["CONTINUE_LEARNING"])


def test_bucket1_excluded_when_activity_stale() -> None:
    ranked = _rank(
        node_ids=["n001"],
        overlays={"n001": _overlay("learning")},
        activities={
            "n001": _activity(last_viewed_at=NOW - timedelta(days=RECENT_ACTIVITY_DAYS + 1))
        },
    )
    assert "n001" not in _by_node(ranked)


def test_bucket2_prerequisite_gap_of_learning_node() -> None:
    # 边 (n001 → n002)：n001 是 n002 的前置；n002 学习中，n001 无状态 → 前置缺口
    ranked = _rank(
        node_ids=["n001", "n002"],
        edges=[("n001", "n002")],
        overlays={"n002": _overlay("learning")},
    )
    by_node = _by_node(ranked)
    assert by_node["n001"] == (2, ["PREREQUISITE_GAP"])
    # n002 没有近期活动，本身不入列
    assert "n002" not in by_node


def test_bucket2_not_applied_when_node_has_state() -> None:
    ranked = _rank(
        node_ids=["n001", "n002"],
        edges=[("n001", "n002")],
        overlays={"n001": _overlay("proficient"), "n002": _overlay("learning")},
    )
    assert "n001" not in _by_node(ranked)


def test_bucket3_successor_of_proficient_without_state() -> None:
    ranked = _rank(
        node_ids=["n001", "n002"],
        edges=[("n001", "n002")],
        overlays={"n001": _overlay("proficient")},
    )
    assert _by_node(ranked)["n002"] == (3, ["NEXT_GRAPH_NODE"])


def test_bucket4_summary_review_signal() -> None:
    ranked = _rank(
        node_ids=["n001"],
        overlays={"n001": _overlay("learning")},
        review_signal_node_ids={"n001"},
    )
    assert _by_node(ranked)["n001"] == (4, ["SUMMARY_MEMORY_SIGNAL"])


def test_bucket5_stale_proficiency() -> None:
    old = NOW - timedelta(days=STALE_PROFICIENCY_DAYS + 10)
    ranked = _rank(
        node_ids=["n001"],
        overlays={"n001": _overlay("proficient", updated_at=old)},
        activities={"n001": _activity(last_viewed_at=old)},
    )
    assert _by_node(ranked)["n001"] == (5, ["STALE_PROFICIENCY"])


def test_bucket5_excluded_when_recently_touched() -> None:
    ranked = _rank(
        node_ids=["n001"],
        overlays={"n001": _overlay("proficient")},
        activities={"n001": _activity(last_check_in_at=NOW - timedelta(days=5))},
    )
    assert "n001" not in _by_node(ranked)


# ---------------------------------------------------------------------------
# expert：默认不推荐，强冲突证据例外（§16.5 优先级 6）
# ---------------------------------------------------------------------------


def test_expert_not_recommended_by_default() -> None:
    ranked = _rank(node_ids=["n001"], overlays={"n001": _overlay("expert")})
    assert ranked == []


def test_expert_recommended_on_strong_conflict() -> None:
    overlay = _overlay(
        "expert",
        evidence=[{"direction": "conflict", "strength": STRONG}],
    )
    ranked = _rank(node_ids=["n001"], overlays={"n001": overlay})
    assert _by_node(ranked)["n001"] == (6, ["REVIEW_AFTER_CONFLICT"])


def test_expert_weak_conflict_not_enough() -> None:
    overlay = _overlay(
        "expert",
        evidence=[{"direction": "conflict", "strength": STRONG - 0.1}],
    )
    ranked = _rank(node_ids=["n001"], overlays={"n001": overlay})
    assert ranked == []


def test_has_strong_conflict_ignores_non_conflict() -> None:
    overlay = _overlay("expert", evidence=[{"direction": "strong_positive", "strength": 1.0}])
    assert has_strong_conflict(overlay, threshold=STRONG) is False


# ---------------------------------------------------------------------------
# 跨桶排序与同桶确定性
# ---------------------------------------------------------------------------


def test_ordering_across_buckets() -> None:
    old = NOW - timedelta(days=STALE_PROFICIENCY_DAYS + 10)
    ranked = _rank(
        node_ids=["n001", "n002", "n003", "n004"],
        edges=[("n002", "n001"), ("n003", "n004")],
        overlays={
            "n001": _overlay("learning"),
            "n003": _overlay("proficient"),
            "n004": _overlay("proficient", updated_at=old),
        },
        activities={
            "n001": _activity(last_viewed_at=NOW - timedelta(days=1)),
            "n004": _activity(last_viewed_at=old),
        },
    )
    # n001 桶 1（学习+近期活动）；n002 桶 2（n001 的前置缺口）；
    # n004 桶 5（长期未复习熟练）；n003 熟练但未过期且无后继推荐 → 不入列
    assert [node_id for node_id, _b, _c, _k in ranked] == ["n001", "n002", "n004"]


def test_multiple_reasons_use_min_bucket_and_sorted_codes() -> None:
    ranked = _rank(
        node_ids=["n001", "n002"],
        edges=[("n001", "n002")],
        overlays={"n002": _overlay("learning")},
        review_signal_node_ids={"n001"},
    )
    # n001 同时是前置缺口（桶 2）与总结复习信号（桶 4），取桶 2，reason 按桶序
    assert _by_node(ranked)["n001"] == (2, ["PREREQUISITE_GAP", "SUMMARY_MEMORY_SIGNAL"])


def test_within_bucket_sorted_by_activity_recency_then_count_then_id() -> None:
    ranked = _rank(
        node_ids=["n001", "n002", "n003"],
        edges=[],
        overlays={f"n00{i}": _overlay("learning") for i in (1, 2, 3)},
        activities={
            "n001": _activity(last_viewed_at=NOW - timedelta(days=2), event_count=1),
            "n002": _activity(last_viewed_at=NOW - timedelta(days=1), event_count=1),
            "n003": _activity(last_viewed_at=NOW - timedelta(days=1), event_count=9),
        },
    )
    assert [node_id for node_id, _b, _c, _k in ranked] == ["n003", "n002", "n001"]
