"""OpenAI Structured Outputs Schema（§9.2 原文）。

模型不生成 user_id、最终 topic_key、绝对路径、SQL、稳定 ID、
expected_version、删除命令或可执行工具调用（§9.2 转换规则）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.memory.contracts.commands import (
    LearnerPatch,
    MasteryPatch,
    MutationPlanDraft,
    MutationPlanResult,
)

__all__ = [
    "CandidateExtractionResult",
    "CandidateMemory",
    "ExtractedEvidence",
    "LearnerPatch",
    "MasteryPatch",
    "MutationPlanDraft",
    "MutationPlanResult",
]


class ExtractedEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_ref: str
    evidence_type: Literal[
        "explicit_user_statement",
        "user_solution",
        "exercise_result",
        "repeated_error",
        "learning_activity",
        "preference_statement",
        "goal_statement",
        "plan_statement",
    ]
    summary: str = Field(max_length=500)
    strength: float = Field(ge=0, le=1)


class CandidateMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_type: Literal["learner", "mastery"]
    topic_title: str | None = Field(default=None, max_length=120)
    category: Literal[
        "preference",
        "goal",
        "plan",
        "understanding",
        "difficulty",
        "misconception",
        "review_advice",
    ]
    summary: str = Field(max_length=1000)
    long_term_value: Literal["save", "review", "ignore"]
    confidence: float = Field(ge=0, le=1)
    evidence: list[ExtractedEvidence] = Field(min_length=1, max_length=20)
    graph_node_candidates: list[str] = Field(default_factory=list, max_length=5)


class CandidateExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[CandidateMemory] = Field(max_length=20)
    ignored_reason_codes: list[str] = Field(default_factory=list)
