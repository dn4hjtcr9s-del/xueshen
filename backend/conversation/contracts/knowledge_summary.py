"""知识总结域的 Pydantic 契约（知识总结方案 §8、§10、§15）。

本模块只定义可序列化的数据结构、字段边界和跨字段校验，不依赖 FastAPI、
Worker、数据库或 OpenAI SDK。服务层必须在模型输出通过本模块校验后，继续执行
来源、用户归属、版本、章节保护和目标 ID 等确定性业务校验。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_CANDIDATES_PER_GENERATION = 4
MAX_CANDIDATE_ITEMS = 20
MAX_CANDIDATE_ALIASES = 5
MAX_SUMMARY_ALIASES = 20
MAX_SECTION_ITEMS = 12
MAX_SUMMARY_ARRAY_ITEMS = 48
MAX_ITEM_SOURCES = 100
MAX_RECALLED_SUMMARIES = 5
MAX_SUMMARY_SOURCES_PAGE = 50

type KnowledgeSection = Literal[
    "definitions", "theorems", "formulas", "properties", "methods", "pitfalls"
]
type AllKnowledgeSection = Literal[
    "overview", "definitions", "theorems", "formulas", "properties", "methods", "pitfalls"
]
type CandidateScope = Literal["math", "non_math", "mixed"]
type CandidateReusableValue = Literal["save", "ignore"]
type KnowledgeItemOrigin = Literal["ai", "user"]
type SourceRole = Literal["user", "assistant"]
type KnowledgeSummaryReviewState = Literal["clean", "possible_duplicate", "conflict"]
type KnowledgeSummaryGenerationTrigger = Literal[
    "auto", "manual", "manual_refresh", "manual_retry", "ops_retry"
]
type KnowledgeSummaryGenerationStatus = Literal[
    "pending",
    "processing",
    "retry_wait",
    "succeeded",
    "no_change",
    "needs_review",
    "dead_letter",
    "cancelled",
]
type ReviewReasonCode = Literal[
    "PROTECTED_SECTION_CONFLICT",
    "CONTRADICTORY_CONTENT",
    "AMBIGUOUS_EXACT_ALIAS",
    "UNSAFE_REPLACE",
    "STALE_TARGET",
]


class KnowledgeSummaryContractModel(BaseModel):
    """知识总结契约的共同基类，拒绝未冻结字段。"""

    model_config = ConfigDict(extra="forbid")


class SourceSupport(KnowledgeSummaryContractModel):
    """模型为候选条目提供的消息级来源短引。"""

    message_id: UUID
    quote: str = Field(min_length=1, max_length=300)


class CandidateItem(KnowledgeSummaryContractModel):
    """尚未物化为总结条目的模型候选。"""

    section: KnowledgeSection
    text: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    supports: list[SourceSupport] = Field(min_length=1, max_length=3)

    @field_validator("supports")
    @classmethod
    def validate_support_message_ids(cls, value: list[SourceSupport]) -> list[SourceSupport]:
        """同一候选条目不得重复引用同一消息。"""
        if len({support.message_id for support in value}) != len(value):
            raise ValueError("同一候选条目的 support message_id 不得重复")
        return value


class KnowledgeCandidate(KnowledgeSummaryContractModel):
    """提取阶段的一张可复用知识候选卡。"""

    scope: CandidateScope
    topic_group_title: str = Field(min_length=1, max_length=160)
    topic_title: str = Field(min_length=1, max_length=240)
    aliases: list[str] = Field(default_factory=list, max_length=MAX_CANDIDATE_ALIASES)
    confidence: float = Field(ge=0, le=1)
    reusable_value: CandidateReusableValue
    overview: CandidateItem | None = None
    items: list[CandidateItem] = Field(default_factory=list, max_length=MAX_CANDIDATE_ITEMS)

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, value: list[str]) -> list[str]:
        """别名仅允许非空的原始显示文本，规范化由后续策略模块负责。"""
        if any(not alias.strip() or len(alias) > 240 for alias in value):
            raise ValueError("知识总结别名必须为 1..240 字符")
        if len({alias.strip() for alias in value}) != len(value):
            raise ValueError("同一候选中的别名不得重复")
        return value


class KnowledgeExtractionResult(KnowledgeSummaryContractModel):
    """OpenAI Structured Outputs 的提取阶段响应。"""

    candidates: list[KnowledgeCandidate] = Field(
        default_factory=list, max_length=MAX_CANDIDATES_PER_GENERATION
    )
    ignored_reason_codes: list[str] = Field(default_factory=list, max_length=20)


class KnowledgeSummaryItem(KnowledgeSummaryContractModel):
    """当前总结 content 中的可编辑条目。"""

    item_id: UUID
    text: str = Field(min_length=1, max_length=1000)
    origin: KnowledgeItemOrigin
    source_ids: list[UUID] = Field(default_factory=list, max_length=MAX_ITEM_SOURCES)

    @model_validator(mode="after")
    def validate_origin_sources(self) -> Self:
        """AI 条目必须保留至少一条消息级来源。"""
        if self.origin == "ai" and not self.source_ids:
            raise ValueError("AI 知识条目必须至少有一个 source_id")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("KnowledgeSummaryItem.source_ids 不得重复")
        return self


class KnowledgeSummaryContent(KnowledgeSummaryContractModel):
    """`knowledge_summaries.content` 的 schema v1。"""

    schema_version: Literal[1] = 1
    overview: KnowledgeSummaryItem | None = None
    definitions: list[KnowledgeSummaryItem] = Field(
        default_factory=list, max_length=MAX_SECTION_ITEMS
    )
    theorems: list[KnowledgeSummaryItem] = Field(default_factory=list, max_length=MAX_SECTION_ITEMS)
    formulas: list[KnowledgeSummaryItem] = Field(default_factory=list, max_length=MAX_SECTION_ITEMS)
    properties: list[KnowledgeSummaryItem] = Field(
        default_factory=list, max_length=MAX_SECTION_ITEMS
    )
    methods: list[KnowledgeSummaryItem] = Field(default_factory=list, max_length=MAX_SECTION_ITEMS)
    pitfalls: list[KnowledgeSummaryItem] = Field(default_factory=list, max_length=MAX_SECTION_ITEMS)

    @model_validator(mode="after")
    def validate_summary_item_ids(self) -> Self:
        """一张总结内的 item_id 全局唯一，避免 PATCH 与合并动作歧义。"""
        items = [
            item
            for section in (
                self.definitions,
                self.theorems,
                self.formulas,
                self.properties,
                self.methods,
                self.pitfalls,
            )
            for item in section
        ]
        if len(items) > MAX_SUMMARY_ARRAY_ITEMS:
            raise ValueError("知识总结条目数量超过上限")
        if len({item.item_id for item in items}) != len(items):
            raise ValueError("同一知识总结中的 item_id 不得重复")
        return self


class SetOverviewMutation(KnowledgeSummaryContractModel):
    action: Literal["set"] = "set"
    reason: str = Field(min_length=1, max_length=300)


class MergeOverviewSourceMutation(KnowledgeSummaryContractModel):
    action: Literal["merge_source"] = "merge_source"
    existing_overview_item_id: UUID
    reason: str = Field(min_length=1, max_length=300)


class ReplaceOverviewMutation(KnowledgeSummaryContractModel):
    action: Literal["replace"] = "replace"
    existing_overview_item_id: UUID
    reason: str = Field(min_length=1, max_length=300)


class IgnoreOverviewMutation(KnowledgeSummaryContractModel):
    action: Literal["ignore"] = "ignore"
    reason: str = Field(min_length=1, max_length=300)


type OverviewMutation = Annotated[
    SetOverviewMutation
    | MergeOverviewSourceMutation
    | ReplaceOverviewMutation
    | IgnoreOverviewMutation,
    Field(discriminator="action"),
]


class AppendItemMutation(KnowledgeSummaryContractModel):
    action: Literal["append"] = "append"
    candidate_item_index: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=300)


class MergeItemSourceMutation(KnowledgeSummaryContractModel):
    action: Literal["merge_source"] = "merge_source"
    candidate_item_index: int = Field(ge=0)
    existing_item_id: UUID
    reason: str = Field(min_length=1, max_length=300)


class ReplaceItemMutation(KnowledgeSummaryContractModel):
    action: Literal["replace"] = "replace"
    candidate_item_index: int = Field(ge=0)
    existing_item_id: UUID
    reason: str = Field(min_length=1, max_length=300)


class IgnoreItemMutation(KnowledgeSummaryContractModel):
    action: Literal["ignore"] = "ignore"
    candidate_item_index: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=300)


type ItemMutation = Annotated[
    AppendItemMutation | MergeItemSourceMutation | ReplaceItemMutation | IgnoreItemMutation,
    Field(discriminator="action"),
]


class CreateSummaryPlan(KnowledgeSummaryContractModel):
    action: Literal["create"] = "create"
    candidate_index: int = Field(ge=0)
    match_confidence: float = Field(ge=0, le=1)
    possible_duplicate_target_ids: list[UUID] = Field(
        default_factory=list, max_length=MAX_RECALLED_SUMMARIES
    )
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("possible_duplicate_target_ids")
    @classmethod
    def validate_possible_duplicate_target_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("possible_duplicate_target_ids 不得重复")
        return value


class MergeSummaryPlan(KnowledgeSummaryContractModel):
    action: Literal["merge"] = "merge"
    candidate_index: int = Field(ge=0)
    target_summary_id: UUID
    target_version: int = Field(ge=1)
    match_confidence: float = Field(ge=0, le=1)
    overview_mutation: OverviewMutation | None = None
    item_mutations: list[ItemMutation] = Field(default_factory=list, max_length=MAX_CANDIDATE_ITEMS)
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("item_mutations")
    @classmethod
    def validate_mutation_indexes(cls, value: list[ItemMutation]) -> list[ItemMutation]:
        indexes = [mutation.candidate_item_index for mutation in value]
        if len(indexes) != len(set(indexes)):
            raise ValueError("同一候选条目不得出现在多个 mutation 中")
        return value


class NoChangeSummaryPlan(KnowledgeSummaryContractModel):
    action: Literal["no_change"] = "no_change"
    candidate_index: int = Field(ge=0)
    target_summary_id: UUID | None = None
    target_version: int | None = Field(default=None, ge=1)
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_target_pair(self) -> Self:
        if (self.target_summary_id is None) != (self.target_version is None):
            raise ValueError(
                "no_change 的 target_summary_id 与 target_version 必须同时存在或同时为空"
            )
        return self


class NeedsReviewSummaryPlan(KnowledgeSummaryContractModel):
    action: Literal["needs_review"] = "needs_review"
    candidate_index: int = Field(ge=0)
    reason_code: ReviewReasonCode
    target_summary_ids: list[UUID] = Field(min_length=1, max_length=MAX_RECALLED_SUMMARIES)
    proposed_overview: str | None = Field(default=None, max_length=800)
    proposed_sections: dict[KnowledgeSection, list[str]] = Field(default_factory=dict)
    reason: str = Field(min_length=1, max_length=300)

    @field_validator("target_summary_ids")
    @classmethod
    def validate_target_summary_ids(cls, value: list[UUID]) -> list[UUID]:
        if len(set(value)) != len(value):
            raise ValueError("needs_review 的 target_summary_ids 不得重复")
        return value

    @field_validator("proposed_sections")
    @classmethod
    def validate_proposed_sections(
        cls, value: dict[KnowledgeSection, list[str]]
    ) -> dict[KnowledgeSection, list[str]]:
        for section, texts in value.items():
            if len(texts) > MAX_SECTION_ITEMS or any(
                not text.strip() or len(text) > 1000 for text in texts
            ):
                raise ValueError(f"{section} 的建议内容不符合长度限制")
        return value


type CandidateMergePlan = Annotated[
    CreateSummaryPlan | MergeSummaryPlan | NoChangeSummaryPlan | NeedsReviewSummaryPlan,
    Field(discriminator="action"),
]


class KnowledgeMergePlanResult(KnowledgeSummaryContractModel):
    """合并规划阶段的完整 Structured Output。"""

    plans: list[CandidateMergePlan] = Field(max_length=MAX_CANDIDATES_PER_GENERATION)

    @field_validator("plans")
    @classmethod
    def validate_candidate_indexes(
        cls, value: list[CandidateMergePlan]
    ) -> list[CandidateMergePlan]:
        indexes = [plan.candidate_index for plan in value]
        if len(indexes) != len(set(indexes)):
            raise ValueError("merge plan 的 candidate_index 不得重复")
        return value


def validate_merge_plan_against_candidates(
    result: KnowledgeMergePlanResult, candidates: list[KnowledgeCandidate]
) -> None:
    """执行与候选集合相关、无法仅靠 JSON Schema 表达的冻结校验。"""
    expected_indexes = set(range(len(candidates)))
    actual_indexes = {plan.candidate_index for plan in result.plans}
    if len(result.plans) != len(candidates) or actual_indexes != expected_indexes:
        raise ValueError("merge plan 必须完整且唯一覆盖过滤后的候选")

    for plan in result.plans:
        candidate = candidates[plan.candidate_index]
        if isinstance(plan, MergeSummaryPlan):
            if candidate.overview is None and plan.overview_mutation is not None:
                raise ValueError("候选没有 overview 时 overview_mutation 必须为 null")
            if candidate.overview is not None and plan.overview_mutation is None:
                raise ValueError("候选有 overview 时必须提供 overview_mutation")
            mutation_indexes = {mutation.candidate_item_index for mutation in plan.item_mutations}
            if mutation_indexes != set(range(len(candidate.items))):
                raise ValueError("merge 的 item_mutations 必须完整覆盖候选条目")
        elif isinstance(plan, CreateSummaryPlan):
            # create 固定物化完整候选内容，模型无权选择性省略条目。
            continue


class KnowledgeSummaryItemEditInput(KnowledgeSummaryContractModel):
    """用户编辑数组章节时提交的条目；空 ID 表示服务端新建。"""

    item_id: UUID | None = None
    text: str = Field(min_length=1, max_length=1000)


class OverviewEditInput(KnowledgeSummaryContractModel):
    """用户编辑 overview 的结构化输入。"""

    item_id: UUID | None = None
    text: str = Field(min_length=1, max_length=800)


class KnowledgeSummaryPatchRequest(KnowledgeSummaryContractModel):
    """PATCH 总结请求；字段缺失由 API 层通过 model_fields_set 区分。"""

    expected_version: int = Field(ge=1)
    topic_group_title: str | None = Field(default=None, min_length=1, max_length=160)
    topic_title: str | None = Field(default=None, min_length=1, max_length=240)
    overview: OverviewEditInput | None = None
    sections: dict[KnowledgeSection, list[KnowledgeSummaryItemEditInput]] = Field(
        default_factory=dict
    )
    unlock_sections: list[AllKnowledgeSection] = Field(default_factory=list, max_length=7)

    @field_validator("sections")
    @classmethod
    def validate_sections(
        cls, value: dict[KnowledgeSection, list[KnowledgeSummaryItemEditInput]]
    ) -> dict[KnowledgeSection, list[KnowledgeSummaryItemEditInput]]:
        for section, items in value.items():
            if len(items) > MAX_SECTION_ITEMS:
                raise ValueError(f"{section} 的条目数量超过上限")
            non_null_ids = [item.item_id for item in items if item.item_id is not None]
            if len(non_null_ids) != len(set(non_null_ids)):
                raise ValueError(f"{section} 中 item_id 不得重复")
        return value

    @model_validator(mode="after")
    def validate_unlock_sections(self) -> Self:
        if len(set(self.unlock_sections)) != len(self.unlock_sections):
            raise ValueError("unlock_sections 不得重复")
        overlap = set(self.unlock_sections) & set(self.sections)
        if "overview" in self.unlock_sections and "overview" in self.model_fields_set:
            overlap.add("overview")
        if overlap:
            raise ValueError("unlock_sections 不得与本次修改章节重叠")
        return self


class CreateKnowledgeSummaryGenerationRequest(KnowledgeSummaryContractModel):
    """手动生成、重试与重新整理的 API 请求。"""

    client_request_id: str = Field(
        min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:-]{1,200}$"
    )
    force: bool = False


class AffectedKnowledgeSummary(KnowledgeSummaryContractModel):
    summary_id: UUID
    topic_group_title: str = Field(min_length=1, max_length=160)
    topic_title: str = Field(min_length=1, max_length=240)


class KnowledgeSummaryGenerationResponse(KnowledgeSummaryContractModel):
    generation_id: UUID
    trigger: KnowledgeSummaryGenerationTrigger
    status: KnowledgeSummaryGenerationStatus
    status_path: str = Field(min_length=1, max_length=500)


class KnowledgeSummaryGenerationStatusResponse(KnowledgeSummaryContractModel):
    generation_id: UUID
    thread_id: UUID
    turn_id: UUID
    trigger: KnowledgeSummaryGenerationTrigger
    status: KnowledgeSummaryGenerationStatus
    affected_summaries: list[AffectedKnowledgeSummary] = Field(default_factory=list)
    warning_codes: list[str] = Field(default_factory=list)
    review_reason_codes: list[ReviewReasonCode] = Field(default_factory=list)
    retryable: bool
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class CurrentTurnKnowledgeSummaryGenerationResponse(KnowledgeSummaryContractModel):
    generation: KnowledgeSummaryGenerationStatusResponse | None = None


class DismissReviewRequest(KnowledgeSummaryContractModel):
    """忽略待确认建议请求。"""

    review_id: UUID


class KnowledgeSummarySourceView(KnowledgeSummaryContractModel):
    """按 Turn 聚合的来源卡；不暴露内部消息级 source_id。"""

    source_turn_id: UUID
    thread_id: UUID
    turn_id: UUID
    status: Literal["available", "unavailable"]
    question_excerpt: str | None = Field(default=None, max_length=300)
    support_message_ids: list[UUID] = Field(default_factory=list)
    support_roles: list[SourceRole] = Field(default_factory=list)
    occurred_at: datetime


class KnowledgeSummarySourcePage(KnowledgeSummaryContractModel):
    items: list[KnowledgeSummarySourceView]
    next_cursor: str | None = None
    has_more: bool


class KnowledgeSummaryListItem(KnowledgeSummaryContractModel):
    summary_id: UUID
    topic_group_title: str
    topic_title: str
    overview_excerpt: str | None = Field(default=None, max_length=280)
    section_counts: dict[AllKnowledgeSection, int]
    source_count: int = Field(ge=0)
    available_source_count: int = Field(ge=0)
    source_message_count: int = Field(ge=0)
    review_state: KnowledgeSummaryReviewState
    version: int = Field(ge=1)
    updated_at: datetime


class KnowledgeSummaryListResponse(KnowledgeSummaryContractModel):
    items: list[KnowledgeSummaryListItem]
    next_cursor: str | None = None
    has_more: bool


class KnowledgeSummaryTopicGroup(KnowledgeSummaryContractModel):
    """当前用户可筛选的大主题聚合项。"""

    key: str
    title: str
    summary_count: int = Field(ge=0)
    updated_at: datetime


class KnowledgeSummaryTopicGroupResponse(KnowledgeSummaryContractModel):
    """大主题分页响应。"""

    items: list[KnowledgeSummaryTopicGroup]
    next_cursor: str | None = None
    has_more: bool


class KnowledgeSummaryStatsResponse(KnowledgeSummaryContractModel):
    """首页和个人中心使用的知识总结统计。"""

    active_count: int = Field(ge=0)
    updated_last_7_days: int = Field(ge=0)
    pending_review_count: int = Field(ge=0)
    available_source_count: int = Field(ge=0)


class PendingReviewView(KnowledgeSummaryContractModel):
    """详情页可展示的结构化待确认建议。"""

    review_id: UUID
    generation_id: UUID
    reason_code: ReviewReasonCode
    proposed_topic_title: str
    proposed_sections: dict[KnowledgeSection, list[str]]
    source_turn_id: UUID
    created_at: datetime


class PossibleDuplicateView(KnowledgeSummaryContractModel):
    """详情页可展示的可能重复关系，标题表示当前卡的对端。"""

    duplicate_id: UUID
    summary_id: UUID
    possible_target_summary_id: UUID
    topic_group_title: str
    topic_title: str
    match_score: float = Field(ge=0, le=1)
    status: Literal["pending", "dismissed", "merged", "resolved"]
    created_at: datetime


class KnowledgeSummaryDetailResponse(KnowledgeSummaryContractModel):
    """知识总结详情、结构化评审和可能重复建议。"""

    summary_id: UUID
    topic_group_title: str
    topic_title: str
    status: Literal["active"]
    review_state: KnowledgeSummaryReviewState
    version: int = Field(ge=1)
    content_schema_version: Literal[1]
    content: KnowledgeSummaryContent
    protected_sections: list[AllKnowledgeSection]
    source_count: int = Field(ge=0)
    available_source_count: int = Field(ge=0)
    source_message_count: int = Field(ge=0)
    last_generated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    pending_review_count: int = Field(ge=0)
    pending_reviews: list[PendingReviewView] = Field(max_length=10)
    possible_duplicates: list[PossibleDuplicateView] = Field(max_length=5)
