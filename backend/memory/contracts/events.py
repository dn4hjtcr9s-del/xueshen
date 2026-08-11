"""Outbox 领域事件契约（规格 §15）。

event_type 与 payload 类型一一绑定（§15：不能只校验为任意 dict）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MemoryDomainEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: str = Field(min_length=1, max_length=100)
    event_version: Literal[1] = 1
    user_id: UUID
    aggregate_type: str = Field(min_length=1, max_length=100)
    aggregate_id: str = Field(min_length=1, max_length=200)
    aggregate_version: int = Field(ge=0)
    occurred_at: datetime
    trace_id: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any]


class MemoryChangedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    memory_id: str = Field(min_length=1, max_length=160)
    memory_type: Literal["mastery"]
    before_version: int | None = Field(default=None, ge=1)
    after_version: int = Field(ge=1)
    topic_key: str = Field(min_length=1, max_length=160)
    graph_projection_candidates: list[str] = Field(default_factory=list, max_length=20)


class MemoryDeletedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    memory_id: str = Field(min_length=1, max_length=160)
    memory_type: Literal["learner", "mastery"]
    deleted_version: int = Field(ge=1)
    restore_until: datetime
    graph_projection_candidates: list[str] = Field(default_factory=list, max_length=20)


class MemoryRestoredPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    memory_id: str = Field(min_length=1, max_length=160)
    memory_type: Literal["learner", "mastery"]
    restored_from_version: int = Field(ge=1)
    after_version: int = Field(ge=1)
    graph_projection_candidates: list[str] = Field(default_factory=list, max_length=20)


class LearnerUpdatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    memory_id: Literal["learner"] = "learner"
    before_version: int | None = Field(default=None, ge=1)
    after_version: int = Field(ge=1)
    changed_sections: list[Literal["preferences", "goals", "plans"]] = Field(max_length=3)


class ReviewCandidateCreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    candidate_id: UUID
    candidate_type: Literal["learner", "mastery", "topic_conflict", "version_conflict"]
    topic_key: str | None = Field(default=None, max_length=160)
    confidence: float = Field(ge=0, le=1)
    created_at: datetime


class ReviewCandidateResolvedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    candidate_id: UUID
    decision: Literal["accepted", "corrected", "rejected", "expired"]
    resolution_target: Literal["merge_existing", "create_new_topic"] | None = None
    target_memory_id: str | None = Field(default=None, max_length=160)
    result_operation_id: UUID | None = None
    resolved_at: datetime


class GraphStateChangedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    node_id: str = Field(pattern=r"^n\d{3,}$")
    before_status: Literal["learning", "proficient", "expert"] | None
    after_status: Literal["learning", "proficient", "expert"] | None
    source: Literal["user", "summary_memory", "system_recompute"]
    explanation_available: bool
    audit_id: UUID


class GraphStateExplanationAvailablePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    node_id: str = Field(pattern=r"^n\d{3,}$")
    audit_id: UUID
    summary: str = Field(min_length=1, max_length=500)
    changed_at: datetime


class AccountMemoryPurgeRequestedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    account_deletion_id: UUID
    user_hash: str = Field(min_length=64, max_length=64)
    requested_at: datetime
    purge_deadline: datetime


EVENT_PAYLOAD_TYPES: dict[str, type[BaseModel]] = {
    "memory.changed": MemoryChangedPayload,
    "memory.deleted": MemoryDeletedPayload,
    "memory.restored": MemoryRestoredPayload,
    "learner.updated": LearnerUpdatedPayload,
    "review_candidate.created": ReviewCandidateCreatedPayload,
    "review_candidate.resolved": ReviewCandidateResolvedPayload,
    "graph_state.changed": GraphStateChangedPayload,
    "graph_state.explanation_available": GraphStateExplanationAvailablePayload,
    "account_memory.purge_requested": AccountMemoryPurgeRequestedPayload,
}


class TypedMemoryDomainEvent(MemoryDomainEvent):
    """payload 已按 event_type 绑定类型校验过的事件信封。"""

    @model_validator(mode="after")
    def validate_payload_type(self) -> TypedMemoryDomainEvent:
        payload_model = EVENT_PAYLOAD_TYPES.get(self.event_type)
        if payload_model is None:
            raise ValueError(f"未知 event_type: {self.event_type}")
        payload_model.model_validate(self.payload)
        return self

    def typed_payload(self) -> BaseModel:
        return EVENT_PAYLOAD_TYPES[self.event_type].model_validate(self.payload)
