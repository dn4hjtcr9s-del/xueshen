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
    node_id: str
    title: str
    status: Literal["learning", "proficient", "expert"] | None
    reason_codes: list[str]


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
