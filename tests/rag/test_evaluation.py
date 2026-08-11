"""RAG 检索评估测试：Recall@K 使用 exact 集合作为基线。"""

from __future__ import annotations

import pytest

from backend.rag.evaluation import recall_at_k


def test_recall_at_k_compares_ann_with_exact_prefix() -> None:
    exact = ["a", "b", "c", "d"]
    approximate = ["a", "x", "c", "y"]

    assert recall_at_k(exact, approximate, k=4) == 0.5
    assert recall_at_k(exact, approximate, k=2) == 0.5


def test_recall_at_k_rejects_invalid_k() -> None:
    with pytest.raises(ValueError, match="k"):
        recall_at_k(["a"], ["a"], k=0)
