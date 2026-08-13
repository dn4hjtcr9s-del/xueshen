"""Conversation Domain 数据契约（方案 §7）。

字段与 conversation 数据库表一一对应；状态机语义（§1.5 R1–R5 / §8.6）：
- Turn status: accepted → running → completed | failed | cancelled
  （accepted 可被取消 API 直接转 cancelled）；
- Thread status: active → deleting → deleted（delete_thread Job 是唯一协调器）；
- Turn Event 序号由 conversation_turns.last_event_sequence 分配；
- Outbox 状态机: pending → processing → delivered / retry_wait / dead_letter。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

TurnStatus = Literal["accepted", "running", "cancelling", "completed", "failed", "cancelled"]
ThreadStatus = Literal["active", "archived", "deleting", "deleted"]
MessageStatus = Literal["completed", "cancelled", "failed", "deleted"]
OutboxStatus = Literal["pending", "processing", "retry_wait", "delivered", "dead_letter"]
JobStatus = Literal["pending", "processing", "retry_wait", "done", "dead_letter"]
JobType = Literal["generate_title", "summarize_thread", "delete_thread"]
MemoryTrigger = Literal["turn_boundary", "explicit_remember"]
MemorySubmissionStatus = Literal["not_required", "pending", "retrying", "accepted", "failed"]

SOURCE_CHECKPOINT_PREFIX = "conv-src-v1:"


class ThreadRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: UUID
    user_id: UUID
    title: str | None = None
    status: ThreadStatus = "active"
    version: int = Field(ge=0)
    last_message_sequence: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    deletion_generation: int = Field(ge=0)


class MessageRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    thread_id: UUID
    turn_id: UUID
    user_id: UUID
    sequence: int = Field(ge=1)
    role: Literal["user", "assistant"]
    content: str
    status: MessageStatus = "completed"
    content_hash: str
    eligible_for_context: bool = True
    eligible_for_memory: bool = True
    occurred_at: datetime
    completed_at: datetime | None = None
    deleted_at: datetime | None = None


class TurnRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    thread_id: UUID
    user_id: UUID
    client_request_id: str = Field(min_length=1, max_length=200)
    request_id: str
    run_id: str
    user_message_id: UUID
    assistant_message_id: UUID | None = None
    status: TurnStatus = "accepted"
    lease_owner: str | None = None
    lease_generation: int = Field(ge=0)
    lease_expires_at: datetime | None = None
    attempt_count: int = Field(ge=0)
    next_attempt_at: datetime
    expected_thread_version: int = Field(ge=0)
    graph_thread_id: str | None = None
    graph_checkpoint_id: str | None = None
    source_checkpoint_id: str | None = None
    plan_revision: int = Field(ge=0)
    memory_trigger: MemoryTrigger = "turn_boundary"
    memory_submission_status: MemorySubmissionStatus = "not_required"
    memory_operation_id: UUID | None = None
    last_event_sequence: int = Field(ge=0)
    degraded_flags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class TurnEventRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    turn_id: UUID
    sequence: int = Field(ge=1)
    event_type: str
    request_id: str
    run_id: str
    occurred_at: datetime
    payload: dict[str, object]


class OutboxRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int = Field(ge=1)
    idempotency_key: str
    user_id: UUID
    thread_id: UUID
    turn_id: UUID | None = None
    message_ids: list[str] = Field(default_factory=list)
    source_checkpoint_id: str | None = None
    trigger: str | None = None
    topic_hints: list[str] = Field(default_factory=list)
    graph_node_hints: list[str] = Field(default_factory=list)
    status: OutboxStatus = "pending"
    attempt_count: int = Field(ge=0)
    next_attempt_at: datetime
    lease_owner: str | None = None
    lease_generation: int = Field(ge=0)
    lease_expires_at: datetime | None = None
    last_error_code: str | None = None
    created_at: datetime
    delivered_at: datetime | None = None


class JobRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: UUID
    job_type: JobType
    thread_id: UUID
    user_id: UUID
    target_sequence: int | None = None
    deletion_generation: int | None = None
    status: JobStatus = "pending"
    attempt_count: int = Field(ge=0)
    next_attempt_at: datetime
    lease_owner: str | None = None
    lease_generation: int = Field(ge=0)
    lease_expires_at: datetime | None = None
    last_error_code: str | None = None
    created_at: datetime
    updated_at: datetime


class SummaryRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: UUID
    sequence: int = Field(ge=1)
    content: str
    token_count: int = Field(ge=0)
    created_at: datetime


def build_source_checkpoint_id(thread_id: UUID, turn_id: UUID, manifest_hash: str) -> str:
    """source_checkpoint_id 格式（§7.2）：conv-src-v1:{thread_id}:{turn_id}:{sha256}。"""
    return f"{SOURCE_CHECKPOINT_PREFIX}{thread_id}:{turn_id}:{manifest_hash}"


def build_source_manifest(thread_id: UUID, turn_id: UUID, rows: list[dict[str, object]]) -> str:
    """canonical source manifest（§7.2 / D9：finalize 与 Reader 共用同一算法）。

    至少包含按 sequence 排序的 message_id / role / sequence / content_hash；
    使用完整 SHA-256，禁止截取前 16 位。返回完整 source_checkpoint_id。
    """
    import hashlib
    import json as _json

    ordered = sorted(rows, key=lambda r: int(str(r["sequence"])))
    manifest = {
        "thread_id": str(thread_id),
        "turn_id": str(turn_id),
        "messages": [
            {
                "message_id": str(row["message_id"]),
                "role": str(row["role"]),
                "sequence": int(str(row["sequence"])),
                "content_hash": str(row["content_hash"]),
            }
            for row in ordered
        ],
    }
    canonical = _json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return build_source_checkpoint_id(
        thread_id, turn_id, hashlib.sha256(canonical.encode()).hexdigest()
    )
