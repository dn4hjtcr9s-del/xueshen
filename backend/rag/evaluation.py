"""RAG 检索质量评估函数：以精确向量查询作为 ANN Recall 基线。"""

from __future__ import annotations

from collections.abc import Sequence


def recall_at_k(exact_ids: Sequence[str], approximate_ids: Sequence[str], *, k: int) -> float:
    """计算 ANN Top-K 对 exact Top-K 的集合召回率。"""
    if k <= 0:
        raise ValueError("k 必须大于 0")
    exact = set(exact_ids[:k])
    if not exact:
        return 1.0
    approximate = set(approximate_ids[:k])
    return len(exact & approximate) / len(exact)
