"""RAG 检索质量评估函数：提供 ANN Recall、主证据与多正例排名指标。"""

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


def primary_rank(primary_id: str, ranked_ids: Sequence[str]) -> int | None:
    """返回主证据首次命中的 1-based 排名；未命中时返回 ``None``。"""
    for rank, chunk_id in enumerate(ranked_ids, start=1):
        if chunk_id == primary_id:
            return rank
    return None


def primary_metrics(
    primary_ids: Sequence[str],
    rankings: Sequence[Sequence[str]],
    *,
    cutoff: int = 20,
) -> dict[str, float]:
    """计算教材主证据的 Primary@1、Primary@5 与 MRR。"""
    if len(primary_ids) != len(rankings):
        raise ValueError("primary_ids 与 rankings 的长度必须一致")
    if not primary_ids:
        raise ValueError("评测集不能为空")
    if cutoff <= 0:
        raise ValueError("cutoff 必须大于 0")

    ranks = [
        primary_rank(primary_id, ranking[:cutoff])
        for primary_id, ranking in zip(primary_ids, rankings, strict=True)
    ]
    return {
        "primary_at_1": sum(rank == 1 for rank in ranks) / len(ranks),
        "primary_at_5": sum(rank is not None and rank <= 5 for rank in ranks) / len(ranks),
        "mrr": sum(1 / rank for rank in ranks if rank is not None) / len(ranks),
    }


def acceptable_rank(
    acceptable_ids: Sequence[str],
    ranked_ids: Sequence[str],
) -> int | None:
    """返回任一完整可接受证据首次命中的 1-based 排名。"""
    if not acceptable_ids:
        raise ValueError("acceptable_ids 不能为空")
    acceptable = set(acceptable_ids)
    for rank, chunk_id in enumerate(ranked_ids, start=1):
        if chunk_id in acceptable:
            return rank
    return None


def acceptable_metrics(
    acceptable_id_groups: Sequence[Sequence[str]],
    rankings: Sequence[Sequence[str]],
    *,
    cutoff: int = 20,
) -> dict[str, float]:
    """计算多正例口径的 Acceptable@1、Acceptable@5 与 MRR。"""
    if len(acceptable_id_groups) != len(rankings):
        raise ValueError("acceptable_id_groups 与 rankings 的长度必须一致")
    if not acceptable_id_groups:
        raise ValueError("评测集不能为空")
    if cutoff <= 0:
        raise ValueError("cutoff 必须大于 0")
    if any(not acceptable_ids for acceptable_ids in acceptable_id_groups):
        raise ValueError("每个 acceptable_ids 正例组都不能为空")

    ranks = [
        acceptable_rank(acceptable_ids, ranking[:cutoff])
        for acceptable_ids, ranking in zip(acceptable_id_groups, rankings, strict=True)
    ]
    return {
        "acceptable_at_1": sum(rank == 1 for rank in ranks) / len(ranks),
        "acceptable_at_5": sum(rank is not None and rank <= 5 for rank in ranks) / len(ranks),
        "mrr": sum(1 / rank for rank in ranks if rank is not None) / len(ranks),
    }
