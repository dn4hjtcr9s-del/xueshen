"""Operation 信封与结果契约（规格 §5.3 / §7.1）。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.memory.contracts.commands import MemoryPayload
from backend.memory.contracts.common import (
    ActorType,
    InputKind,
    OperationStatus,
    OperationType,
)
from backend.memory.contracts.errors import PublicError


class MemoryOperation(BaseModel):
    """MemoryManagerGraph 的稳定输入信封（§5.3）。"""

    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    idempotency_key: str = Field(min_length=1, max_length=200)
    user_id: UUID
    actor_type: ActorType
    input_kind: InputKind
    operation_type: OperationType
    priority: int = Field(ge=0, le=100)
    occurred_at: datetime
    payload: MemoryPayload
    trace_id: str = Field(min_length=32, max_length=64)
    graph_thread_id: str
    schema_version: Literal[1] = 1


class MutationResult(BaseModel):
    mutation_id: UUID
    memory_id: str
    action: Literal[
        "create",
        "merge",
        "replace",
        "append_evidence",
        "forget",
        "restore",
    ]
    before_version: int | None
    after_version: int | None


class GraphStateChangeView(BaseModel):
    node_id: str = Field(pattern=r"^n\d{3,}$")
    before_status: Literal["learning", "proficient", "expert"] | None
    after_status: Literal["learning", "proficient", "expert"] | None
    before_version: int | None
    after_version: int | None
    source_type: Literal["user", "summary_memory", "system_recompute"]
    reason_codes: list[str] = Field(default_factory=list, max_length=20)
    changed_at: datetime


class MemoryOperationResult(BaseModel):
    operation_id: UUID
    status: OperationStatus
    operation_type: OperationType
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    cancelled_at: datetime | None = None
    mutations: list[MutationResult] = Field(default_factory=list)
    review_candidate_ids: list[UUID] = Field(default_factory=list)
    graph_state_changes: list[GraphStateChangeView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: PublicError | None = None
