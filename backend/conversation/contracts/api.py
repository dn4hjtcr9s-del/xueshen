"""Conversation REST API 与 SSE 契约（方案 §17）。

冻结：REST 请求/响应形状、cursor 分页、SSE 事件 envelope 与严格 payload、
输入上限。所有 Schema 均为 extra="forbid"，事件 payload 只允许前端渲染必需字段。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.memory.contracts.errors import PublicError

# ---------------------------------------------------------------------------
# 输入上限（§17.2 / §4 Q4）
# ---------------------------------------------------------------------------

MAX_USER_MESSAGE_CHARS = 10_000
MAX_CITATION_SNIPPET_CHARS = 300
DEFAULT_LIST_LIMIT = 50
MAX_LIST_LIMIT = 100
MAX_FOLLOWUPS = 3
SSE_DELTA_BATCH_CHARS = 64
SSE_DELTA_BATCH_MS = 100

# 合法事件类型（§17.4；新增 turn.progress 仅公开安全的阶段摘要）
ConversationEventType = Literal[
    "turn.accepted",
    "turn.started",
    "turn.progress",
    "answer.delta",
    "citation.available",
    "turn.degraded",
    "memory.submission",
    "answer.completed",
    "turn.failed",
    "turn.cancelled",
]

# turn.degraded 合法 flags（§17.4.1）
DegradedFlag = Literal[
    "memory_unavailable",
    "memory_degraded",
    "retrieval_partial",
    "retrieval_unavailable",
    "citation_degraded",
]


# ---------------------------------------------------------------------------
# Citation DTO（§13.3）
# ---------------------------------------------------------------------------


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation_id: str  # 如 C1
    corpus_id: str
    chunk_ids: list[str] = Field(max_length=3)
    book_id: str
    book_name: str
    chapter_path: list[str]
    page_start: int | None = None
    page_end: int | None = None
    snippet: str = Field(max_length=MAX_CITATION_SNIPPET_CHARS)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    matched_subquery_ids: list[str] = Field(default_factory=list, max_length=6)


# ---------------------------------------------------------------------------
# REST API（§17.1 / §17.2 / §17.3）
# ---------------------------------------------------------------------------


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=200)


class CreateConversationResponse(BaseModel):
    thread_id: UUID
    version: int = Field(ge=0)


class ThreadListItem(BaseModel):
    thread_id: UUID
    title: str
    status: Literal["active", "archived", "deleting", "deleted"]
    version: int = Field(ge=0)
    updated_at: datetime


class ConversationListResponse(BaseModel):
    items: list[ThreadListItem]
    next_cursor: str | None = None
    has_more: bool


class MessageView(BaseModel):
    message_id: UUID
    thread_id: UUID
    turn_id: UUID
    role: Literal["user", "assistant"]
    content: str
    status: Literal["completed", "cancelled", "failed", "deleted"]
    sequence: int = Field(ge=1)
    occurred_at: datetime
    completed_at: datetime | None = None


class ConversationDetailResponse(BaseModel):
    thread_id: UUID
    title: str
    version: int = Field(ge=0)
    status: Literal["active", "archived", "deleting", "deleted"]
    messages: list[MessageView]
    next_cursor: str | None = None
    has_more: bool


class CreateTurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=MAX_USER_MESSAGE_CHARS)
    expected_thread_version: int = Field(ge=0)


class CreateTurnResponse(BaseModel):
    thread_id: UUID
    turn_id: UUID
    user_message_id: UUID
    thread_version: int = Field(ge=0)
    status: Literal["accepted", "running", "cancelling", "completed", "failed", "cancelled"]
    event_stream_path: str


class TurnStatusResponse(BaseModel):
    turn_id: UUID
    thread_id: UUID
    status: Literal["accepted", "running", "cancelling", "completed", "failed", "cancelled"]
    thread_version: int = Field(ge=0)
    assistant_message_id: UUID | None = None
    error: PublicError | None = None
    event_stream_path: str


# ---------------------------------------------------------------------------
# SSE Envelope 与固定 payload（§17.4）
# ---------------------------------------------------------------------------


class SSEEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    event_id: str
    sequence: int = Field(ge=1)
    event_type: ConversationEventType
    request_id: str
    thread_id: UUID
    turn_id: UUID
    run_id: str
    occurred_at: datetime
    data: dict[str, Any]


class TurnAcceptedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    user_message_id: UUID


class TurnStartedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["running"] = "running"


ProgressStage = Literal[
    "context",
    "memory",
    "rewrite",
    "retrieval",
    "rerank",
    "evidence",
    "answer",
]
ProgressStatus = Literal["started", "completed", "skipped", "degraded"]


class TurnProgressPayload(BaseModel):
    """公开的流水线阶段摘要；禁止承载隐藏推理或系统提示词。"""

    model_config = ConfigDict(extra="forbid")

    stage: ProgressStage
    status: ProgressStatus
    title: str = Field(min_length=1, max_length=80)
    detail: str | None = Field(default=None, max_length=500)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


class AnswerDeltaPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text_delta: str


class CitationAvailablePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    citation: Citation


class TurnDegradedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    flags: list[DegradedFlag] = Field(max_length=16)


class MemorySubmissionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pending", "accepted", "retrying", "failed"]
    operation_id: UUID | None = None


class AnswerCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_message_id: UUID
    thread_version: int = Field(ge=0)
    answer: str
    citations: list[Citation] = Field(default_factory=list, max_length=20)
    followups: list[str] = Field(default_factory=list, max_length=MAX_FOLLOWUPS)
    degraded_flags: list[DegradedFlag] = Field(default_factory=list, max_length=16)


class TurnFailedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    error: PublicError


class TurnCancelledPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["cancelled"] = "cancelled"
    partial_answer_available: bool
