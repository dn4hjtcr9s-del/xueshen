"""ConversationGraph 输入、State、RewritePlan、EvidenceAssessment 与 Answer 契约。

对应方案 §10/§11/§14/§15。Graph 不接受浏览器直接传入的 Memory、检索命中、
安全过滤条件或内部身份；运行时对象不进入 Checkpoint（§10.4）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.conversation.contracts.api import Citation

# ---------------------------------------------------------------------------
# Graph Input（§10.1）
# ---------------------------------------------------------------------------


class ConversationGraphInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    thread_id: UUID
    turn_id: UUID
    user_message_id: UUID
    request_id: str
    run_id: str
    expected_thread_version: int = Field(ge=0)


# ---------------------------------------------------------------------------
# RewritePlan（§11.1）
# ---------------------------------------------------------------------------

AnswerMode = Literal["direct", "memory_assisted", "rag"]
MemoryTrigger = Literal["none", "explicit_remember"]

# 检索裁决：retrieve（需要教材检索）/ skip（无需检索直接回答）/ clarify（先澄清）
RetrievalDecisionType = Literal["retrieve", "skip", "clarify"]

# 低基数、可组合的裁决依据标签：不枚举具体业务场景，只描述"为什么"的因素。
RetrievalBasisCode = Literal[
    # 必须依赖教材事实（定义、公式、定理、证明、例题来源）
    "TEXTBOOK_FACT_REQUIRED",
    # 用户明确要求查教材、出处、引用或资料
    "EXPLICIT_SOURCE_REQUESTED",
    # 问题目标是用户个人掌握/薄弱/学习状态
    "USER_STATE_TARGET",
    # 回答需要用户记忆或学习记录作为证据
    "MEMORY_SOURCE_REQUIRED",
    # 已有长期记忆足以支撑回答
    "MEMORY_CONTEXT_SUFFICIENT",
    # 已有当前对话上下文足以支撑回答
    "CONVERSATION_CONTEXT_SUFFICIENT",
    # 教材不能证明用户个人状态（教材不是个人掌握情况的证据）
    "TEXTBOOK_NOT_EVIDENCE_FOR_USER_STATE",
    # 当前上下文不足
    "CURRENT_CONTEXT_INSUFFICIENT",
    # 请求存在无法安全消解的歧义
    "AMBIGUOUS_REQUEST",
    # 仅用于服务端降级：规划器不可用
    "PLANNER_UNAVAILABLE",
    # 仅用于服务端降级：规划功能关闭
    "PLANNER_DISABLED",
]


class RetrievalDecision(BaseModel):
    """改写 Agent 对教材检索的裁决及其可展示依据（含前端进度展示）。

    ``basis_codes`` 是低基数、可组合的决策依据标签，不枚举具体业务场景；
    ``rationale`` 是给前端展示的自然语言说明，不承载隐藏思维链。
    """

    model_config = ConfigDict(extra="forbid")

    decision: RetrievalDecisionType
    basis_codes: list[RetrievalBasisCode] = Field(default_factory=list, max_length=6)
    rationale: str = Field(min_length=1, max_length=200)


class RetrievalSubquery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subquery_id: str = Field(min_length=1, max_length=200)
    query_text: str = Field(min_length=1, max_length=500)
    intent: str = Field(default="", max_length=100)
    coverage_target: str = Field(default="", max_length=200)
    semantic_filters: dict[str, list[str]] = Field(default_factory=dict)


class RewritePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    plan_revision: int = Field(ge=0)
    standalone_question: str = Field(min_length=1, max_length=1000)
    # fail-safe 默认值：不默认检索，检索必须由 retrieval_decision 明确裁决
    answer_mode: AnswerMode = "direct"
    need_retrieval: bool = False
    retrieval_decision: RetrievalDecision
    memory_trigger: MemoryTrigger = "none"
    topic_hints: list[str] = Field(default_factory=list, max_length=20)
    subqueries: list[RetrievalSubquery] = Field(default_factory=list, max_length=6)
    # 兼容既有降级观测字段；模型决策依据以 retrieval_decision 为准。
    reason_codes: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_retrieval_contract(self) -> RewritePlan:
        """校验裁决、布尔路由、回答模式与子问题的一致性。

        该校验既约束模型输出，也约束 Fake/兼容 Gateway 的结果；生产 Gateway
        会在结构化输出重试阶段捕获同类 Schema 校验失败。
        """
        decision = self.retrieval_decision.decision
        has_subqueries = bool(self.subqueries)
        if decision == "retrieve":
            if not self.need_retrieval or self.answer_mode != "rag" or not has_subqueries:
                raise ValueError(
                    "retrieve 裁决必须同时满足 need_retrieval=true、answer_mode=rag 且有子问题"
                )
        else:
            if self.need_retrieval or self.answer_mode == "rag" or has_subqueries:
                raise ValueError("skip/clarify 裁决必须不检索、非 rag 且 subqueries 为空")
        return self


# ---------------------------------------------------------------------------
# EvidenceAssessment（§14.1）
# ---------------------------------------------------------------------------


class EvidenceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["sufficient", "needs_more", "insufficient"] = "sufficient"
    covered_aspects: list[str] = Field(default_factory=list, max_length=20)
    missing_aspects: list[str] = Field(default_factory=list, max_length=20)
    unsupported_claim_risk: Literal["low", "medium", "high"] = "low"
    next_search_focus: list[str] = Field(default_factory=list, max_length=20)
    reason_codes: list[str] = Field(default_factory=list, max_length=10)


# ---------------------------------------------------------------------------
# Answer 输出（§15.4 / §15.5）
# ---------------------------------------------------------------------------


class AnswerEvidenceAnnotation(BaseModel):
    """回答合同中的证据标注，说明 chunk 与 task 的确定性关联。"""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(min_length=1, max_length=200)
    chunk_ids: list[str] = Field(default_factory=list, max_length=20)
    task_ids: list[str] = Field(default_factory=list, max_length=20)
    roles: list[str] = Field(default_factory=list, max_length=10)
    relevance_notes: list[str] = Field(default_factory=list, max_length=20)
    coverage: Literal["covered", "partial", "unassigned"] = "unassigned"


class AnswerTaskContract(BaseModel):
    """回答合同中的子任务，绑定任务要求、证据和局部缺证据状态。"""

    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=200)
    subquery_id: str | None = Field(default=None, max_length=200)
    task_type: str = Field(default="", max_length=100)
    question: str = Field(min_length=1, max_length=500)
    required: bool = True
    evidence_ids: list[str] = Field(default_factory=list, max_length=30)
    evidence_roles: list[str] = Field(default_factory=list, max_length=10)
    status: Literal["covered", "partially_covered", "missing"] = "missing"
    missing_aspects: list[str] = Field(default_factory=list, max_length=20)


class AnswerSubquestion(BaseModel):
    """回答合同中的问题拆分结果。"""

    model_config = ConfigDict(extra="forbid")

    subquery_id: str = Field(min_length=1, max_length=200)
    question: str = Field(min_length=1, max_length=500)
    intent: str = Field(default="", max_length=100)
    coverage_target: str = Field(default="", max_length=200)


class AnswerHistoryItem(BaseModel):
    """回答所需的最小历史条目。"""

    model_config = ConfigDict(extra="forbid")

    role: str = Field(min_length=1, max_length=30)
    content: str = Field(min_length=1)


class AnswerMemoryContext(BaseModel):
    """回答合同中的相关长期记忆；仅用于个性化，不作为教材证据。"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["available", "degraded", "unavailable"] = "unavailable"
    learner: dict[str, object] = Field(default_factory=dict)
    mastery: list[dict[str, object]] = Field(default_factory=list)
    truncated: bool = False


class AnswerEvidenceBudget(BaseModel):
    """回答证据预算的分配和实际选取结果。"""

    model_config = ConfigDict(extra="forbid")

    total_budget: int = Field(ge=0)
    used_tokens: int = Field(ge=0)
    truncated_tokens: int = Field(ge=0)
    task_budgets: dict[str, int] = Field(default_factory=dict)
    role_budgets: dict[str, dict[str, int]] = Field(default_factory=dict)
    task_selected_evidence_ids: dict[str, list[str]] = Field(default_factory=dict)
    selected_evidence_ids: list[str] = Field(default_factory=list)
    dropped_evidence_ids: list[str] = Field(default_factory=list)


class AnswerContract(BaseModel):
    """回答输入合同：问题、任务、必要上下文、证据边界和预算分配。"""

    model_config = ConfigDict(extra="forbid")

    current_question: str = Field(min_length=1, max_length=10_000)
    standalone_question: str = Field(min_length=1, max_length=1000)
    subquestions: list[AnswerSubquestion] = Field(default_factory=list, max_length=6)
    necessary_history: list[AnswerHistoryItem] = Field(default_factory=list, max_length=22)
    relevant_memory: AnswerMemoryContext = Field(default_factory=AnswerMemoryContext)
    tasks: list[AnswerTaskContract] = Field(default_factory=list, max_length=6)
    evidence_annotations: list[AnswerEvidenceAnnotation] = Field(
        default_factory=list, max_length=30
    )
    partial_refusal_rules: list[str] = Field(default_factory=list, max_length=10)
    evidence_budget: AnswerEvidenceBudget


class AnswerGenerationOutput(BaseModel):
    """模型生成契约；Citation 由服务端证据集确定性注入。"""

    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    followups: list[str] = Field(default_factory=list, max_length=3)


class AnswerPayload(BaseModel):
    """最终对外回答契约，包含服务端生成的 Citation。"""

    model_config = ConfigDict(extra="forbid")

    answer: str
    citations: list[Citation] = Field(default_factory=list, max_length=20)
    followups: list[str] = Field(default_factory=list, max_length=3)


# ---------------------------------------------------------------------------
# TurnContextSnapshot（§9）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotMemory:
    status: Literal["available", "degraded", "unavailable"] = "unavailable"
    learner: dict[str, object] = field(default_factory=dict)
    mastery: list[dict[str, object]] = field(default_factory=list)
    graph_states: list[dict[str, object]] = field(default_factory=list)
    recommendations: list[dict[str, object]] = field(default_factory=list)
    truncated: bool = False
    fetched_at: datetime | None = None
    #: memory-rebuild §2.4 D1：prime 模式注入的固定提示词（summary + 注册表目录）。
    #: 可选字段，缺省空 dict —— flag 关闭时快照与提示词都与既有实现逐字一致。
    prime: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class SnapshotBudgets:
    history_tokens: int = 6000
    memory_tokens: int = 3000
    retrieval_tokens: int = 4000
    answer_tokens: int = 2000


@dataclass(frozen=True)
class SnapshotMessage:
    message_id: UUID
    role: str
    sequence: int
    content: str


@dataclass(frozen=True)
class TurnContextSnapshot:
    """不可变快照（§9）：问题改写与回答必须使用同一个实例。"""

    snapshot_id: str
    snapshot_version: str = "1"
    created_at: datetime = field(default_factory=datetime.now)
    user_id: UUID | None = None
    thread_id: UUID | None = None
    turn_id: UUID | None = None
    current_message: str = ""
    recent_messages: list[SnapshotMessage] = field(default_factory=list)
    conversation_summary: str | None = None
    memory: SnapshotMemory = field(default_factory=SnapshotMemory)
    budgets: SnapshotBudgets = field(default_factory=SnapshotBudgets)
    context_hash: str = ""

    def with_context_hash(self, context_hash: str) -> TurnContextSnapshot:
        """返回带 context_hash 的新实例（保持不可变）。"""
        return TurnContextSnapshot(
            snapshot_id=self.snapshot_id,
            snapshot_version=self.snapshot_version,
            created_at=self.created_at,
            user_id=self.user_id,
            thread_id=self.thread_id,
            turn_id=self.turn_id,
            current_message=self.current_message,
            recent_messages=self.recent_messages,
            conversation_summary=self.conversation_summary,
            memory=self.memory,
            budgets=self.budgets,
            context_hash=context_hash,
        )
