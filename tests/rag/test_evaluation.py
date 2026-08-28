"""RAG 检索评估测试：覆盖 ANN Recall、主证据与多正例排名指标。"""

from __future__ import annotations

import pytest

from backend.rag.evaluation import (
    acceptable_metrics,
    acceptable_rank,
    primary_metrics,
    primary_rank,
    recall_at_k,
)


def test_recall_at_k_compares_ann_with_exact_prefix() -> None:
    exact = ["a", "b", "c", "d"]
    approximate = ["a", "x", "c", "y"]

    assert recall_at_k(exact, approximate, k=4) == 0.5
    assert recall_at_k(exact, approximate, k=2) == 0.5


def test_recall_at_k_rejects_invalid_k() -> None:
    with pytest.raises(ValueError, match="k"):
        recall_at_k(["a"], ["a"], k=0)


def test_primary_rank_returns_first_match() -> None:
    assert primary_rank("p", ["x", "p", "p"]) == 2
    assert primary_rank("p", ["x", "y"]) is None


def test_primary_metrics_counts_first_and_fifth_place_hits() -> None:
    metrics = primary_metrics(
        ["first", "fifth"],
        [
            ["first", "a", "b", "c", "d"],
            ["a", "b", "c", "d", "fifth"],
        ],
        cutoff=5,
    )

    assert metrics == {
        "primary_at_1": 0.5,
        "primary_at_5": 1.0,
        "mrr": pytest.approx((1.0 + 0.2) / 2),
    }


def test_primary_metrics_treats_outside_cutoff_and_missing_as_zero() -> None:
    metrics = primary_metrics(
        ["outside", "missing"],
        [
            ["a", "b", "c", "d", "e", "outside"],
            ["a", "b", "c"],
        ],
        cutoff=5,
    )

    assert metrics == {"primary_at_1": 0.0, "primary_at_5": 0.0, "mrr": 0.0}


def test_primary_metrics_uses_first_duplicate_rank() -> None:
    metrics = primary_metrics(["p"], [["x", "p", "p"]], cutoff=3)

    assert metrics == {
        "primary_at_1": 0.0,
        "primary_at_5": 1.0,
        "mrr": 0.5,
    }


def test_primary_metrics_rejects_empty_dataset() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        primary_metrics([], [])


def test_primary_metrics_rejects_length_mismatch() -> None:
    with pytest.raises(ValueError, match="长度"):
        primary_metrics(["a"], [])


def test_primary_metrics_rejects_invalid_cutoff() -> None:
    with pytest.raises(ValueError, match="cutoff"):
        primary_metrics(["a"], [["a"]], cutoff=0)


def test_acceptable_rank_returns_first_match_across_multiple_positives() -> None:
    assert acceptable_rank(["primary", "equivalent"], ["x", "equivalent", "primary"]) == 2
    assert acceptable_rank(["primary", "equivalent"], ["x", "y"]) is None


def test_acceptable_metrics_counts_any_complete_evidence_hit() -> None:
    metrics = acceptable_metrics(
        [["primary-a", "equivalent-a"], ["primary-b", "equivalent-b"]],
        [
            ["equivalent-a", "x", "primary-a"],
            ["x", "y", "z", "w", "primary-b"],
        ],
        cutoff=5,
    )

    assert metrics == {
        "acceptable_at_1": 0.5,
        "acceptable_at_5": 1.0,
        "mrr": pytest.approx((1.0 + 0.2) / 2),
    }


def test_acceptable_metrics_treats_outside_cutoff_as_zero() -> None:
    metrics = acceptable_metrics(
        [["primary", "equivalent"]],
        [["a", "b", "c", "d", "e", "equivalent"]],
        cutoff=5,
    )

    assert metrics == {"acceptable_at_1": 0.0, "acceptable_at_5": 0.0, "mrr": 0.0}


def test_acceptable_metrics_rejects_empty_positive_group() -> None:
    with pytest.raises(ValueError, match="正例组"):
        acceptable_metrics([[]], [["a"]])


def test_acceptable_metrics_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        acceptable_metrics([], [])
    with pytest.raises(ValueError, match="长度"):
        acceptable_metrics([["a"]], [])
    with pytest.raises(ValueError, match="cutoff"):
        acceptable_metrics([["a"]], [["a"]], cutoff=0)
