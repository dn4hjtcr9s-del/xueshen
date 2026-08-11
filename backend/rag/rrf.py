"""RRF 排名融合：合并向量、FTS 和公式召回并应用内容检索权重。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """单个召回通道中的候选；序列位置即该通道排名。"""

    chunk_id: str
    retrieval_weight: float
    payload: Any = None


@dataclass(frozen=True, slots=True)
class FusedCandidate:
    """完成去重和权重调整后的 RRF 候选。"""

    chunk_id: str
    score: float
    retrieval_weight: float
    matched_channels: tuple[str, ...]
    payload: Any = None


def fuse_ranked_results(
    channels: Mapping[str, Sequence[RankedCandidate]],
    *,
    rrf_k: int = 60,
    channel_weights: Mapping[str, float] | None = None,
) -> tuple[FusedCandidate, ...]:
    """按 `weight / (rrf_k + rank)` 融合，再乘 chunk 的 retrieval_weight。"""
    if rrf_k <= 0:
        raise ValueError("rrf_k 必须大于 0")
    weights = channel_weights or {}
    scores: dict[str, float] = {}
    candidates: dict[str, RankedCandidate] = {}
    matched: dict[str, set[str]] = {}
    for channel_name, ranked in channels.items():
        seen_in_channel: set[str] = set()
        channel_weight = float(weights.get(channel_name, 1.0))
        for rank, candidate in enumerate(ranked, start=1):
            if candidate.chunk_id in seen_in_channel:
                continue
            seen_in_channel.add(candidate.chunk_id)
            existing = candidates.get(candidate.chunk_id)
            if existing is not None and existing.retrieval_weight != candidate.retrieval_weight:
                raise ValueError(f"Chunk {candidate.chunk_id} 在不同通道的 retrieval_weight 不一致")
            candidates[candidate.chunk_id] = candidate
            scores[candidate.chunk_id] = scores.get(candidate.chunk_id, 0.0) + (
                channel_weight / (rrf_k + rank)
            )
            matched.setdefault(candidate.chunk_id, set()).add(channel_name)

    fused = [
        FusedCandidate(
            chunk_id=chunk_id,
            score=score * candidates[chunk_id].retrieval_weight,
            retrieval_weight=candidates[chunk_id].retrieval_weight,
            matched_channels=tuple(sorted(matched[chunk_id])),
            payload=candidates[chunk_id].payload,
        )
        for chunk_id, score in scores.items()
    ]
    fused.sort(key=lambda item: (-item.score, item.chunk_id))
    return tuple(fused)
