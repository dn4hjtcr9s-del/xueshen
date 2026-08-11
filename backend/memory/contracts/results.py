"""查询返回结构（规格 §7 / §12.3 / §19.4 / §19.6）。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.memory.contracts.commands import CandidateContentView

T = TypeVar("T")


class CursorPage(BaseModel, Generic[T]):  # noqa: UP046 保留规格 §19.4 原始 Generic 形式
    items: list[T]
    next_cursor: str | None
    has_more: bool


class LearnerMemoryView(BaseModel):
    memory_type: Literal["learner"] = "learner"
    memory_id: Literal["learner"] = "learner"
    version: int = Field(ge=1)
    preferences: list[str]
    goals: list[str]
    plans: list[str]
    evidence_refs: list[str] = Field(max_length=100)
    confidence: float | None = Field(default=None, ge=0, le=1)
    updated_at: datetime


class MasteryMemoryView(BaseModel):
    memory_type: Literal["mastery"] = "mastery"
    memory_id: str = Field(pattern=r"^mastery:.+")
    topic_key: str = Field(min_length=1, max_length=160)
    topic_title: str = Field(min_length=1, max_length=240)
    version: int = Field(ge=1)
    overview: str
    understood: list[str]
    difficulties: list[str]
    review_advice: list[str]
    evidence_refs: list[str] = Field(max_length=100)
    confidence: float | None = Field(default=None, ge=0, le=1)
    updated_at: datetime


#: memory_type 作为 Pydantic discriminator（v1.1 裁决 2）
MemoryDocumentView = Annotated[
    LearnerMemoryView | MasteryMemoryView,
    Field(discriminator="memory_type"),
]


class MemoryIndexEntryView(BaseModel):
    memory_id: str
    memory_type: Literal["learner", "mastery"]
    topic_key: str | None
    title: str
    version: int
    updated_at: datetime


class MemoryIndexView(BaseModel):
    version: int = Field(ge=0)
    entries: list[MemoryIndexEntryView]
    updated_at: datetime | None
    stale: bool


class ReviewCandidateView(BaseModel):
    candidate_id: UUID
    candidate_type: Literal["learner", "mastery", "topic_conflict", "version_conflict"]
    base_memory_id: str | None
    base_version: int | None
    topic_key: str | None
    candidate_content: CandidateContentView
    evidence_refs: list[str] = Field(max_length=100)
    confidence: float = Field(ge=0, le=1)
    status: Literal["pending", "accepted", "corrected", "rejected", "expired"]
    resolution_target: Literal["merge_existing", "create_new_topic"] | None
    target_memory_id: str | None
    resolved_operation_id: UUID | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DeletedMemoryItem(BaseModel):
    memory_id: str
    memory_type: Literal["learner", "mastery"]
    topic_key: str | None
    title: str
    deleted_version: int
    deleted_at: datetime
    restore_until: datetime


class MemorySearchHit(BaseModel):
    memory_id: str
    memory_type: Literal["learner", "mastery"]
    topic_key: str | None
    title: str
    summary: str
    matched_excerpt: str | None
    evidence_refs: list[str] = Field(max_length=100)
    version: int
    updated_at: datetime
    confidence: float | None
    score: float


class MemorySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    topic_keys: list[str] = Field(default_factory=list, max_length=20)
    memory_types: list[Literal["learner", "mastery"]] = Field(default_factory=list)
    cursor: str | None = Field(default=None, max_length=1000)
    limit: int = Field(default=10, ge=1, le=50)


class MemoryNotification(BaseModel):
    notification_id: UUID
    event_type: str
    title: str
    body: str
    aggregate_type: str
    aggregate_id: str
    read_at: datetime | None
    created_at: datetime


class MemoryNotificationPage(CursorPage[MemoryNotification]):
    unread_count: int = Field(ge=0)
