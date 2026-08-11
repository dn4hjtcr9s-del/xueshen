"""总结记忆到图谱节点的确定性映射（规格 §16.4）。

节点映射优先级：
1. 上游 graph_node_hints 且节点存在 → explicit_hint, confidence 1.0
2. topic_title 与节点标题规范化后精确匹配 → exact_alias
3. knowledge_graph_node_aliases 中规范化 alias 精确匹配 → exact_alias
4. 标题和 alias 的原始 pg_trgm 分数达到阈值 → model_candidate

mapping_confidence 由代码计算，不由模型直接生成。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from backend.memory.knowledge_graph.registry import (
    KnowledgeGraphRegistry,
    normalize_graph_title,
)
from backend.settings import Settings

MappingMethod = Literal["explicit_hint", "exact_alias", "model_candidate"]


@dataclass(frozen=True)
class NodeMapping:
    node_id: str
    method: MappingMethod
    confidence: float


async def resolve_node_mapping(
    registry: KnowledgeGraphRegistry,
    settings: Settings,
    *,
    topic_title: str | None,
    graph_node_hints: list[str],
    model_candidate_node_ids: list[str],
) -> NodeMapping | None:
    """按优先级解析节点映射；不满足高阈值规则时返回 None（no_change）。"""
    # 1. 上游明确 hint
    for hint in graph_node_hints:
        if await registry.node_exists(hint):
            return NodeMapping(node_id=hint, method="explicit_hint", confidence=1.0)

    normalized = normalize_graph_title(topic_title) if topic_title else None

    # 2/3. 规范化标题或 alias 精确匹配
    if normalized:
        exact = await registry.find_by_normalized_title(normalized)
        if len(exact) == 1:
            return NodeMapping(
                node_id=str(exact[0]["node_id"]), method="exact_alias", confidence=1.0
            )
        if len(exact) > 1:
            # 同名节点无法消歧时不自动映射（§16.4）
            return None

    # 4. pg_trgm 候选：模型只能给候选 node ID 列表，分数由代码计算
    if normalized:
        candidates = await registry.trgm_candidates(normalized, limit=5)
        if model_candidate_node_ids:
            allowed = set(model_candidate_node_ids)
            candidates = [c for c in candidates if str(c["node_id"]) in allowed] or candidates
        if candidates:
            best = float(candidates[0]["score"])
            second = float(candidates[1]["score"]) if len(candidates) > 1 else 0.0
            if (
                best >= settings.graph_projection_mapping_min
                and best - second >= settings.graph_projection_mapping_margin
            ):
                return NodeMapping(
                    node_id=str(candidates[0]["node_id"]),
                    method="model_candidate",
                    confidence=best,
                )
    return None
