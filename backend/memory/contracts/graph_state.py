"""知识图谱查询返回结构（规格 §16.5 / §19.5）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class GraphNodeView(BaseModel):
    node_id: str = Field(pattern=r"^n\d{3,}$")
    title: str
    group_key: str | None
    metadata: dict[str, Any]


class GraphEdgeView(BaseModel):
    from_node_id: str
    to_node_id: str
    relation_type: Literal["prerequisite"]


class KnowledgeGraphSnapshot(BaseModel):
    nodes: list[GraphNodeView]
    edges: list[GraphEdgeView]
    manifest_checksum: str = Field(min_length=64, max_length=64)
    synced_at: datetime


class GraphOverlayView(BaseModel):
    node_id: str
    status: Literal["learning", "proficient", "expert"] | None
    version: int | None
    status_source: Literal["user", "summary_memory", "system_recompute"] | None
    updated_at: datetime | None


class GraphNodeDetailView(BaseModel):
    node: GraphNodeView
    overlay: GraphOverlayView
    prerequisite_node_ids: list[str]
    successor_node_ids: list[str]


class GraphRecommendation(BaseModel):
    node_id: str
    title: str
    status: Literal["learning", "proficient", "expert"] | None
    reason_codes: list[
        Literal[
            "CONTINUE_LEARNING",
            "PREREQUISITE_GAP",
            "NEXT_GRAPH_NODE",
            "REVIEW_AFTER_CONFLICT",
            "SUMMARY_MEMORY_SIGNAL",
            "STALE_PROFICIENCY",
        ]
    ]
    prerequisite_node_ids: list[str]
    related_memory_ids: list[str]
    updated_at: datetime | None


class GraphStateExplanation(BaseModel):
    node_id: str
    current_status: Literal["learning", "proficient", "expert"] | None
    explanation_available: bool
    summary: str | None
    reason_codes: list[str]
    source_type: Literal["user", "summary_memory", "system_recompute"] | None
    source_memory_id: str | None
    source_memory_version: int | None
    evidence_refs: list[str] = Field(max_length=10)
    changed_at: datetime | None
