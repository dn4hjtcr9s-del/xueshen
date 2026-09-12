"""学习上下文组装返回结构（规格 §12.5）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.memory.contracts.graph_state import GraphRecommendation


class LearningContextRequest(BaseModel):
    """POST /memory/context 请求体。

    §19 未显式列出该路由；形状按 §12.4/§12.5 与 §18.2 memory:context 推导：
    query 必填，topic_keys 限定主题范围，token_budget 省略时用
    settings.memory_context_token_budget（默认 3000，范围 500–8000）。
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    topic_keys: list[str] = Field(default_factory=list, max_length=20)
    token_budget: int | None = Field(default=None, ge=500, le=8000)


class LearningContextLearner(BaseModel):
    preferences: list[str]
    goals: list[str]
    plans: list[str]
    version: int
    updated_at: datetime
    evidence_refs: list[str] = Field(max_length=100)


class LearningContextMastery(BaseModel):
    memory_id: str
    topic_key: str
    title: str
    overview: str
    understood: list[str]
    difficulties: list[str]
    review_advice: list[str]
    version: int
    updated_at: datetime
    evidence_refs: list[str] = Field(max_length=100)


class LearningContextGraphState(BaseModel):
    """图谱口径的一条注入状态（§12.4 优先级 3）。

    memory-rebuild §3.5②/§5.9 的双源协调结果附加在既有字段之后的四个**可选**键上：
    ``state_source`` / ``conflict`` / ``conflict_reason`` / ``memory_updated_at``
    与 ``kg_updated_at``。既有键的类型与含义不变，未做协调时四个键保持缺省，
    老调用方序列化结果与之前逐字一致。
    """

    node_id: str
    title: str
    status: Literal["learning", "proficient", "expert"] | None
    reason_codes: list[str]
    #: 最终注入值来自哪一路："memory"（长期记忆 mastery）或 "kg"（用户 overlay）。
    #: 未启用双源协调（未传 mastery 侧信息）时为 None。
    state_source: Literal["memory", "kg"] | None = None
    #: 两路状态不一致（含"更新鲜者胜出"与"并列同刻"）时为 True，不静默丢差异。
    conflict: bool = False
    #: 冲突原因，形如 "memory_newer_version:2>1"、"tie:kg_unresolved_conflict"。
    conflict_reason: str | None = None
    #: 长期记忆侧的最新文档版本。
    memory_version: int | None = None
    #: KG 侧 overlay 已经消化到的 mastery 版本（``source_memory_version``）。
    kg_projected_version: int | None = None
    #: 长期记忆侧的最新版本时间（memory 侧更新时间）。
    memory_updated_at: datetime | None = None
    #: KG 侧 overlay 的更新时间。
    kg_updated_at: datetime | None = None


class LearningContextTokenUsage(BaseModel):
    budget: int = Field(ge=0)
    estimated: int = Field(ge=0)
    remaining: int = Field(ge=0)


class LearningContext(BaseModel):
    user_id: UUID
    query: str
    learner: LearningContextLearner | None
    mastery: list[LearningContextMastery]
    graph_states: list[LearningContextGraphState]
    recommendations: list[GraphRecommendation]
    token_usage: LearningContextTokenUsage
    truncated: bool
