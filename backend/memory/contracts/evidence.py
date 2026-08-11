"""证据与数据源契约（规格 §6.1 / §17.3 / §17.4）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConversationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["conversation_evidence"] = "conversation_evidence"
    thread_id: str = Field(min_length=1, max_length=200)
    checkpoint_id: str | None = Field(default=None, max_length=200)
    message_ids: list[str] = Field(min_length=1, max_length=200)
    trigger: Literal[
        "explicit_remember",
        "turn_boundary",
        "topic_switch",
        "exercise_completed",
        "conversation_end",
    ]
    topic_hints: list[str] = Field(default_factory=list, max_length=20)
    graph_node_hints: list[str] = Field(default_factory=list, max_length=20)


class ActivityEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["activity_evidence"] = "activity_evidence"
    activity_type: Literal[
        "forum_post",
        "forum_reply",
        "wrong_question_upload",
        "exercise_attempt",
        "review_result",
        "page_view",
        "bookmark",
        "check_in",
    ]
    activity_ids: list[str] = Field(min_length=1, max_length=200)
    content_ref: str | None = Field(default=None, max_length=500)
    aggregated_count: int = Field(default=1, ge=1, le=10_000)
    window_started_at: datetime | None = None
    window_ended_at: datetime | None = None
    topic_hints: list[str] = Field(default_factory=list, max_length=20)
    graph_node_hints: list[str] = Field(default_factory=list, max_length=20)


# ---------------------------------------------------------------------------
# SourceBundle（§17.4）
# ---------------------------------------------------------------------------

SOURCE_BUNDLE_MAX_BYTES = 80_000
SOURCE_ITEM_CONTENT_MAX = 20_000
SOURCE_ITEM_METADATA_MAX_BYTES = 4096
SOURCE_ITEM_METADATA_MAX_KEYS = 50


class SourceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_ref: str
    role: Literal["user", "assistant", "tool", "activity"]
    content: str = Field(max_length=SOURCE_ITEM_CONTENT_MAX)
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metadata")
    @classmethod
    def validate_metadata_limits(cls, value: dict[str, Any]) -> dict[str, Any]:
        """单项 metadata 最多 4096 bytes、最多 50 个 key（§17.4）。"""
        if len(value) > SOURCE_ITEM_METADATA_MAX_KEYS:
            raise ValueError(f"metadata 超过 {SOURCE_ITEM_METADATA_MAX_KEYS} 个 key")
        from backend.memory.contracts.common import canonical_json

        try:
            size = len(canonical_json(value).encode("utf-8"))
        except TypeError as exc:
            raise ValueError("metadata 必须为 JSON 可序列化结构") from exc
        if size > SOURCE_ITEM_METADATA_MAX_BYTES:
            raise ValueError(f"metadata 超过 {SOURCE_ITEM_METADATA_MAX_BYTES} bytes")
        return value


class SourceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[SourceItem] = Field(max_length=200)
    deleted_refs: list[str] = Field(default_factory=list)
    total_utf8_bytes: int = Field(ge=0, le=SOURCE_BUNDLE_MAX_BYTES)

    @classmethod
    def from_items(
        cls, items: list[SourceItem], deleted_refs: list[str] | None = None
    ) -> SourceBundle:
        """按去重后内容真实计算 total_utf8_bytes（§17.4 裁决 21）。"""
        seen: set[bytes] = set()
        total = 0
        for item in items:
            encoded = item.content.encode("utf-8")
            if encoded in seen:
                continue
            seen.add(encoded)
            total += len(encoded)
        if total > SOURCE_BUNDLE_MAX_BYTES:
            from backend.memory.contracts.errors import SourceTooLargeError

            raise SourceTooLargeError(f"SourceBundle 超过 {SOURCE_BUNDLE_MAX_BYTES} bytes")
        return cls(items=items, deleted_refs=deleted_refs or [], total_utf8_bytes=total)


# ---------------------------------------------------------------------------
# Source deletion（§17.3）
# ---------------------------------------------------------------------------


class SourceDeletedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    source_system: Literal["conversation", "activity"]
    source_ref: str = Field(min_length=1, max_length=500)
    source_version: str | None = Field(default=None, max_length=200)
    deleted_at: datetime


# ---------------------------------------------------------------------------
# 图谱投影证据（§6.4）
# ---------------------------------------------------------------------------


class GraphProjectionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidence_ref: str
    direction: Literal["learning", "positive", "strong_positive", "conflict"]
    strength: float = Field(ge=0, le=1)
    occurred_at: datetime
