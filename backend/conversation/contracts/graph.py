"""ConversationGraph 输入、State、RewritePlan、EvidenceAssessment 与 Answer 契约。

对应方案 §10/§11/§14/§15。Graph 不接受浏览器直接传入的 Memory、检索命中、
安全过滤条件或内部身份；运行时对象不进入 Checkpoint（§10.4）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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
    answer_mode: AnswerMode = "rag"
    need_retrieval: bool = True
    memory_trigger: MemoryTrigger = "none"
    topic_hints: list[str] = Field(default_factory=list, max_length=20)
    subqueries: list[RetrievalSubquery] = Field(default_factory=list, max_length=6)
    reason_codes: list[str] = Field(default_factory=list, max_length=10)


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
