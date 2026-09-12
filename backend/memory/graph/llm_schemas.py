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
    "ConsolidationAliasMerge",
    "ConsolidationConflict",
    "ConsolidationKeywordSet",
    "ConsolidationResult",
    "ConsolidationTopicRoute",
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
    #: memory-rebuild §3.6②：候选涉及的相邻主题，供 planner 生成 `[[link]]`。
    #: 只写主题名（不带 `[[...]]`），不改变候选自身的主体归属。
    related_topic_hints: list[str] = Field(default_factory=list, max_length=5)


class CandidateExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[CandidateMemory] = Field(max_length=20)
    ignored_reason_codes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# consolidation（memory-rebuild §5.9① 末段）
# ---------------------------------------------------------------------------


class ConsolidationTopicRoute(BaseModel):
    """主题路由行：``<topic_key> | <一句话掌握状态> | 熟练度``。"""

    model_config = ConfigDict(extra="forbid")

    topic_key: str = Field(min_length=1, max_length=120)
    status_line: str = Field(min_length=1, max_length=200)
    proficiency: Literal["learning", "proficient", "expert"]


class ConsolidationAliasMerge(BaseModel):
    """近义主题归并候选（§5.9③：必须可回滚、可人工纠错，不得静默删除原命名）。"""

    model_config = ConfigDict(extra="forbid")

    canonical_topic_key: str = Field(min_length=1, max_length=120)
    merged_topic_keys: list[str] = Field(min_length=1, max_length=8)
    reason: str = Field(min_length=1, max_length=300)


class ConsolidationKeywordSet(BaseModel):
    """某个主题的判别性检索词（§2.3 index 注册表的 keywords 来源）。"""

    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(min_length=1, max_length=160)
    keywords: list[str] = Field(min_length=1, max_length=8)


class ConsolidationConflict(BaseModel):
    """并列冲突标注（决议 D 组：更新鲜者优先 + 并列标注，不静默覆盖）。"""

    model_config = ConfigDict(extra="forbid")

    memory_ids: list[str] = Field(min_length=1, max_length=8)
    description: str = Field(min_length=1, max_length=300)


class ConsolidationResult(BaseModel):
    """``summary_consolidate_v1`` 的结构化输出。

    **只有这三个摘要段 + 四类治理输出**，模型不生成版本号、路径、稳定 ID 或执行动作；
    落盘与治理动作全部由服务端代码完成（§9.2 转换规则的同一纪律）。
    """

    model_config = ConfigDict(extra="forbid")

    user_profile: list[str] = Field(default_factory=list, max_length=20)
    stable_preferences: list[str] = Field(default_factory=list, max_length=20)
    topic_routes: list[ConsolidationTopicRoute] = Field(default_factory=list, max_length=200)
    alias_merges: list[ConsolidationAliasMerge] = Field(default_factory=list, max_length=20)
    keywords: list[ConsolidationKeywordSet] = Field(default_factory=list, max_length=200)
    conflicts: list[ConsolidationConflict] = Field(default_factory=list, max_length=20)
