"""用户命令、图谱命令与维护 payload（规格 §6.2–§6.6 / §9.2）。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.memory.contracts.evidence import (
    ActivityEvidence,
    ConversationEvidence,
    GraphProjectionEvidence,
)

# ---------------------------------------------------------------------------
# 用户记忆命令（§6.2）
# ---------------------------------------------------------------------------


class LearnerReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacement_type: Literal["learner"] = "learner"
    preferences: list[str] = Field(default_factory=list, max_length=50)
    goals: list[str] = Field(default_factory=list, max_length=50)
    plans: list[str] = Field(default_factory=list, max_length=50)


class MasteryReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    replacement_type: Literal["mastery"] = "mastery"
    topic_title: str = Field(min_length=1, max_length=120)
    overview: str = Field(default="", max_length=1200)
    understood: list[str] = Field(default_factory=list, max_length=50)
    difficulties: list[str] = Field(default_factory=list, max_length=50)
    review_advice: list[str] = Field(default_factory=list, max_length=30)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)


MemoryReplacement = Annotated[
    LearnerReplacement | MasteryReplacement,
    Field(discriminator="replacement_type"),
]


class CorrectMemoryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["correct_memory"] = "correct_memory"
    memory_id: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(ge=1)
    replacement: MemoryReplacement
    reason: str | None = Field(default=None, max_length=500)


class ForgetMemoryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["forget_memory"] = "forget_memory"
    memory_id: str = Field(min_length=1, max_length=160)
    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)


class RestoreMemoryCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["restore_memory"] = "restore_memory"
    memory_id: str = Field(min_length=1, max_length=160)
    deleted_version: int = Field(ge=1)


class OverrideLearnerProfileCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["override_learner_profile"] = "override_learner_profile"
    expected_version: int | None = Field(default=None, ge=1)
    preferences: list[str] | None = Field(default=None, max_length=50)
    goals: list[str] | None = Field(default=None, max_length=50)
    plans: list[str] | None = Field(default=None, max_length=50)
    reason: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# 候选审核命令（§6.3）
# ---------------------------------------------------------------------------


class CandidateContentView(BaseModel):
    """候选内容的受控结构化联合（v1.1 裁决 7）。"""

    model_config = ConfigDict(extra="forbid")

    memory_type: Literal["learner", "mastery"]
    topic_key: str | None = Field(default=None, max_length=160)
    topic_title: str | None = Field(default=None, max_length=240)
    overview: str | None = Field(default=None, max_length=1200)
    preferences: list[str] = Field(default_factory=list, max_length=50)
    goals: list[str] = Field(default_factory=list, max_length=50)
    plans: list[str] = Field(default_factory=list, max_length=50)
    understood: list[str] = Field(default_factory=list, max_length=50)
    difficulties: list[str] = Field(default_factory=list, max_length=50)
    review_advice: list[str] = Field(default_factory=list, max_length=30)


class ReviewCandidateCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["review_candidate"] = "review_candidate"
    candidate_id: UUID
    decision: Literal["accept", "correct", "reject"]
    resolution_target: Literal["merge_existing", "create_new_topic"] | None = None
    target_memory_id: str | None = Field(default=None, max_length=160)
    corrected_content: MemoryReplacement | None = None
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_review_fields(self) -> ReviewCandidateCommand:
        if self.decision == "correct" and self.corrected_content is None:
            raise ValueError("corrected_content is required for correct")
        if self.decision in {"accept", "reject"} and self.corrected_content is not None:
            raise ValueError("corrected_content is only allowed for correct")
        if self.resolution_target == "merge_existing" and not self.target_memory_id:
            raise ValueError("target_memory_id is required for merge_existing")
        if self.resolution_target == "create_new_topic" and self.target_memory_id is not None:
            raise ValueError("target_memory_id is forbidden for create_new_topic")
        if self.resolution_target is None and self.target_memory_id is not None:
            raise ValueError("target_memory_id requires resolution_target=merge_existing")
        return self


# ---------------------------------------------------------------------------
# 图谱命令和派生更新（§6.4）
# ---------------------------------------------------------------------------


class GraphStateCommand(BaseModel):
    """内部命令：kind/node_id 由 Gateway 固定注入，不是公开请求 schema。"""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["set_graph_state"] = "set_graph_state"
    node_id: str = Field(pattern=r"^n\d{3,}$")
    action: Literal["mark_unfamiliar", "mark_familiar", "clear"]
    expected_version: int | None = Field(default=None, ge=1)


class GraphStatePutRequest(BaseModel):
    """图谱状态 PUT 的公开请求体；node_id 由 URL 路径注入内部命令。"""

    model_config = ConfigDict(extra="forbid")

    action: Literal["mark_unfamiliar", "mark_familiar"]
    expected_version: int | None = Field(default=None, ge=1)


class ProjectSummaryToGraphCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["project_summary_to_graph"] = "project_summary_to_graph"
    trigger_event_type: Literal[
        "memory.changed",
        "memory.deleted",
        "memory.restored",
    ]
    projection_action: Literal[
        "apply_active_version",
        "recompute_without_deleted_version",
    ]
    source_memory_id: str
    source_version: int = Field(ge=1)
    node_id: str = Field(pattern=r"^n\d{3,}$")
    mapping_method: Literal["explicit_hint", "exact_alias", "model_candidate"] | None = None
    mapping_confidence: float | None = Field(default=None, ge=0, le=1)
    evidence: list[GraphProjectionEvidence] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_projection_fields(self) -> ProjectSummaryToGraphCommand:
        """跨字段校验（§6.4）。"""
        if self.trigger_event_type in {"memory.changed", "memory.restored"}:
            if self.projection_action != "apply_active_version":
                raise ValueError("memory.changed/memory.restored 必须使用 apply_active_version")
            if self.mapping_method is None or self.mapping_confidence is None:
                raise ValueError("apply_active_version 要求 mapping_method/mapping_confidence")
            if not self.evidence:
                raise ValueError("apply_active_version 至少需要一条 evidence")
        else:
            if self.projection_action != "recompute_without_deleted_version":
                raise ValueError("memory.deleted 必须使用 recompute_without_deleted_version")
        return self


# ---------------------------------------------------------------------------
# Maintenance（§6.5）
# ---------------------------------------------------------------------------


class MaintenanceCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal[
        "rebuild_index",
        "verify_checksums",
        "purge_tombstones",
        "cleanup_orphan_versions",
        "cleanup_checkpoints",
        "purge_account_memory",
    ]
    target_user_id: UUID | None = None
    dry_run: bool = False
    cursor: str | None = None
    batch_size: int = Field(default=100, ge=1, le=1000)


# ---------------------------------------------------------------------------
# 判别联合（§6.6）
# ---------------------------------------------------------------------------

MemoryPayload = Annotated[
    ConversationEvidence
    | ActivityEvidence
    | CorrectMemoryCommand
    | ForgetMemoryCommand
    | RestoreMemoryCommand
    | OverrideLearnerProfileCommand
    | ReviewCandidateCommand
    | GraphStateCommand
    | ProjectSummaryToGraphCommand
    | MaintenanceCommand,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# 模型输出 → 确定计划（§9.2）
# ---------------------------------------------------------------------------


class LearnerPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preferences_to_add: list[str] = Field(default_factory=list, max_length=20)
    preferences_to_remove: list[str] = Field(default_factory=list, max_length=20)
    goals_to_add: list[str] = Field(default_factory=list, max_length=20)
    goals_to_remove: list[str] = Field(default_factory=list, max_length=20)
    plans_to_add: list[str] = Field(default_factory=list, max_length=20)
    plans_to_remove: list[str] = Field(default_factory=list, max_length=20)


class MasteryPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overview: str | None = Field(default=None, max_length=1200)
    understood_to_add: list[str] = Field(default_factory=list, max_length=30)
    difficulties_to_add: list[str] = Field(default_factory=list, max_length=30)
    difficulties_to_resolve: list[str] = Field(default_factory=list, max_length=30)
    review_advice_to_add: list[str] = Field(default_factory=list, max_length=20)
    evidence_refs_to_add: list[str] = Field(default_factory=list, max_length=50)


class MutationPlanDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_memory_type: Literal["learner", "mastery"]
    topic_title: str | None = Field(default=None, max_length=120)
    action: Literal["create", "merge", "replace", "append_evidence", "no_change"]
    learner_patch: LearnerPatch | None = None
    mastery_patch: MasteryPatch | None = None
    candidate_indexes: list[int] = Field(default_factory=list, max_length=20)
    reasoning_summary: str = Field(max_length=500)


class MutationPlanResult(BaseModel):
    plans: list[MutationPlanDraft] = Field(max_length=8)


class CommitMutationPlan(BaseModel):
    """应用代码转换后的确定计划：代码注入并发令牌与稳定 ID。"""

    model_config = ConfigDict(extra="forbid")

    mutation_id: UUID
    memory_id: str = Field(min_length=1, max_length=160)
    target_memory_type: Literal["learner", "mastery"]
    topic_title: str | None = Field(default=None, max_length=120)
    action: Literal[
        "create",
        "merge",
        "replace",
        "append_evidence",
        "forget",
        "restore",
    ]
    expected_version: int | None = Field(default=None, ge=1)
    deleted_version: int | None = Field(default=None, ge=1)
    learner_patch: LearnerPatch | None = None
    mastery_patch: MasteryPatch | None = None
    candidate_indexes: list[int] = Field(default_factory=list, max_length=20)
    replacement: MemoryReplacement | None = None
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_plan_consistency(self) -> CommitMutationPlan:
        """target_memory_type、patch 类型和 memory_id 必须一致（§9.2 规则 5）。"""
        if self.target_memory_type == "learner":
            if self.memory_id != "learner":
                raise ValueError("learner 计划的 memory_id 必须为 learner")
            if self.mastery_patch is not None:
                raise ValueError("learner 计划不允许 mastery_patch")
            if self.replacement is not None and self.replacement.replacement_type != "learner":
                raise ValueError("replacement 类型与目标不一致")
        else:
            if not self.memory_id.startswith("mastery:"):
                raise ValueError("mastery 计划的 memory_id 必须为 mastery:{topic_key}")
            if self.learner_patch is not None:
                raise ValueError("mastery 计划不允许 learner_patch")
            if self.replacement is not None and self.replacement.replacement_type != "mastery":
                raise ValueError("replacement 类型与目标不一致")
        if self.action == "create" and (
            self.expected_version is not None or self.deleted_version is not None
        ):
            raise ValueError("create 不允许携带并发令牌")
        if self.action == "restore":
            if self.expected_version is not None or self.deleted_version is None:
                raise ValueError("restore 要求 deleted_version 且无 expected_version")
        if self.action in {"merge", "replace", "append_evidence", "forget"}:
            if self.expected_version is None or self.deleted_version is not None:
                raise ValueError("非 create/restore 动作要求 expected_version 且无 deleted_version")
        return self


class IndexRebuildPlan(BaseModel):
    """index.md 确定性重建计划（§8.6.1），不加入模型生成的 CommitMutationPlan。"""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    expected_dirty_at: datetime
    source_versions: dict[str, int]
    action: Literal["rebuild_index"] = "rebuild_index"
