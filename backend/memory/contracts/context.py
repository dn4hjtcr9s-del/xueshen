"""学习上下文组装返回结构（规格 §12.5）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from backend.memory.contracts.graph_state import GraphRecommendation


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
