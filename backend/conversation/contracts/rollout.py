"""Conversation Rollout JSONL 契约（memory-rebuild 计划 §1.5 / §5.2 Phase 0-A）。

短期记忆的事实源是 thread 级 JSONL：每行一条 :class:`RolloutRecord`，字段为
``recorded_at``（writer 实际落盘时间，UTC 毫秒）、``ordinal``（文件级单调递增）、
``type``（记录类型）、``turn_id``（所属轮次）与类型化 ``payload``。

本模块只回答"一行是什么"（契约 + 严格校验）；"什么该落盘"由白名单决定，落在
``backend/conversation/rollout/policy.py``（Phase 1 实现）。两类信息分离的原因：
契约需要长期稳定并进快照测试，白名单会随链路演进而调整。

记录类型集合在 Phase 0 一次定全（含 Phase 5 才写入的 ``memory_prime`` /
``memory_tool_call`` / ``memory_tool_result``），避免后续 Phase 反复改动契约。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from backend.conversation.contracts.domain import TurnStatus

#: JSONL 行格式版本；与首行 ``thread_meta.schema_version`` 同源。
ROLLOUT_SCHEMA_VERSION = 1

#: 文件名前缀：``rollout-<thread创建时间>-<thread_id>.jsonl``（§1.5）。
ROLLOUT_FILE_PREFIX = "rollout"

#: 记忆工具每轮调用上限（§2.7-③ 决议 B 组：独立预算，不共享检索预算）。
MEMORY_TOOL_CALL_BUDGET = 6

RolloutRecordType = Literal[
    "thread_meta",
    "turn_started",
    "turn_completed",
    "turn_context_snapshot",
    "rewrite_plan",
    "evidence_set",
    "embedded_queries",
    "user_message",
    "assistant_message",
    "memory_prime",
    "memory_tool_call",
    "memory_tool_result",
]

#: 白色名单全集（policy.py 与测试共用；未知类型一律拒绝）。
ROLLOUT_RECORD_TYPES: frozenset[str] = frozenset(
    {
        "thread_meta",
        "turn_started",
        "turn_completed",
        "turn_context_snapshot",
        "rewrite_plan",
        "evidence_set",
        "embedded_queries",
        "user_message",
        "assistant_message",
        "memory_prime",
        "memory_tool_call",
        "memory_tool_result",
    }
)

#: 仅 thread 级、不隶属任何 turn 的记录类型。
_THREAD_LEVEL_TYPES: frozenset[str] = frozenset({"thread_meta"})

MemoryToolName = Literal["memory.search", "memory.read"]


class RolloutContractError(ValueError):
    """rollout 行不满足契约（未知类型、字段缺失、越界、时间格式非法）。"""


def format_recorded_at(value: datetime) -> str:
    """UTC 毫秒 ``...SS.sssZ``（对应 codex ``OffsetDateTime::now_utc()``）。"""
    utc = value.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond // 1000:03d}Z"


# ---------------------------------------------------------------------------
# 类型化 payload（§1.5 白名单表；"小文本放全文、大对象放引用"）
# ---------------------------------------------------------------------------


class ThreadMetaPayload(BaseModel):
    """首行元数据（对应 codex SessionMeta，§1.2 表"首行 SessionMeta"行）。

    ``created_at`` 与文件名中的 thread 创建时间同源，但保留毫秒精度。
    """

    model_config = ConfigDict(extra="forbid")

    thread_id: UUID
    user_id: UUID
    created_at: datetime
    graph_version: str = Field(min_length=1, max_length=64)
    schema_version: int = Field(default=ROLLOUT_SCHEMA_VERSION, ge=1)

    @field_validator("created_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("created_at 必须带时区（UTC）")
        return value.astimezone(UTC)


class TurnStartedPayload(BaseModel):
    """轮次开始（段边界，§1.5「turn_started / turn_completed 作为段边界」）。"""

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    request_id: str = Field(min_length=1, max_length=200)
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("started_at 必须带时区（UTC）")
        return value.astimezone(UTC)


class TurnCompletedPayload(BaseModel):
    """轮次终态：必须带 status 与降级标记（§5.3 写入顺序第 6 条）。"""

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    status: TurnStatus
    degraded_flags: list[str] = Field(default_factory=list, max_length=50)
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("completed_at 必须带时区（UTC）")
        return value.astimezone(UTC)


class TurnContextSnapshotPayload(BaseModel):
    """摘要级上下文快照（§1.11-② 决议 C 组：维持摘要 + 引用，不落全文）。"""

    model_config = ConfigDict(extra="forbid")

    snapshot_hash: str = Field(min_length=1, max_length=128)
    message_ids: list[str] = Field(default_factory=list, max_length=500)
    token_estimate: int = Field(ge=0)
    memory_status: str = Field(min_length=1, max_length=50)


class RewritePlanPayload(BaseModel):
    """重写计划，每 revision 一条（§1.5 白名单表）。"""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    plan: dict[str, Any]


class EvidenceSetPayload(BaseModel):
    """证据集合只落引用与顺序（§1.5：chunk 正文在 rag 库，体积大且非短期记忆）。"""

    model_config = ConfigDict(extra="forbid")

    references: list[str] = Field(default_factory=list, max_length=500)


class EmbeddedQueriesPayload(BaseModel):
    """向量化查询只记模型标识 + 维度（§1.5 白名单表）。"""

    model_config = ConfigDict(extra="forbid")

    model: str = Field(min_length=1, max_length=200)
    dimensions: int = Field(ge=1, le=100_000)


class MessagePayload(BaseModel):
    """``user_message`` / ``assistant_message`` 正文全文（KB 级，全文廉价）。"""

    model_config = ConfigDict(extra="forbid")

    message_id: UUID
    sequence: int = Field(ge=1)
    role: Literal["user", "assistant"]
    content: str
    content_hash: str = Field(min_length=1, max_length=128)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def _require_tz(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at 必须带时区（UTC）")
        return value.astimezone(UTC)


class MemoryPrimePayload(BaseModel):
    """首轮 prime 快照（§2.4 D2：pin 住的固定提示词，压缩后原样重注入）。"""

    model_config = ConfigDict(extra="forbid")

    summary_hash: str = Field(min_length=1, max_length=128)
    schema_version: str = Field(min_length=1, max_length=20)
    generated_at: datetime | None = None
    truncated: bool = False
    index_entry_count: int = Field(default=0, ge=0, le=100_000)


class MemoryToolCallPayload(BaseModel):
    """工具调用摘要（§5.7：记录摘要/引用，不记录完整大对象或 secret）。"""

    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(min_length=1, max_length=100)
    tool: MemoryToolName
    call_index: int = Field(ge=1, le=MEMORY_TOOL_CALL_BUDGET)
    arguments: dict[str, Any] = Field(default_factory=dict)


class MemoryToolResultPayload(BaseModel):
    """工具结果摘要；正文留在 documents，此处只留引用与版本（§5.7）。"""

    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(min_length=1, max_length=100)
    tool: MemoryToolName
    status: Literal["ok", "error", "budget_exceeded"]
    result_count: int = Field(default=0, ge=0, le=1000)
    truncated: bool = False
    document_versions: list[str] = Field(default_factory=list, max_length=100)
    error_code: str | None = Field(default=None, max_length=100)


#: 记录类型 → payload 模型（未知类型即契约违例）。
PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "thread_meta": ThreadMetaPayload,
    "turn_started": TurnStartedPayload,
    "turn_completed": TurnCompletedPayload,
    "turn_context_snapshot": TurnContextSnapshotPayload,
    "rewrite_plan": RewritePlanPayload,
    "evidence_set": EvidenceSetPayload,
    "embedded_queries": EmbeddedQueriesPayload,
    "user_message": MessagePayload,
    "assistant_message": MessagePayload,
    "memory_prime": MemoryPrimePayload,
    "memory_tool_call": MemoryToolCallPayload,
    "memory_tool_result": MemoryToolResultPayload,
}


# ---------------------------------------------------------------------------
# 行契约
# ---------------------------------------------------------------------------


class RolloutPointer(BaseModel):
    """消息 → rollout 段的定位指针（§5.2 Phase 0-A；落 conversation_messages 列）。"""

    model_config = ConfigDict(extra="forbid")

    segment_id: UUID
    ordinal: int = Field(ge=0)
    byte_offset_start: int = Field(ge=0)
    byte_offset_end: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_range(self) -> RolloutPointer:
        if self.byte_offset_end <= self.byte_offset_start:
            raise ValueError("byte_offset_end 必须大于 byte_offset_start")
        return self


class RolloutRecord(BaseModel):
    """一行 JSONL。

    ``ordinal`` 为文件级单调序号（与 turn 内 event sequence 作用域不同，§1.5）；
    ``recorded_at`` 是 writer 实际落盘时间，不是事件发生时间。
    """

    model_config = ConfigDict(extra="forbid")

    recorded_at: datetime
    ordinal: int = Field(ge=0)
    type: RolloutRecordType
    turn_id: UUID | None = None
    payload: dict[str, Any]

    @field_validator("recorded_at")
    @classmethod
    def _normalize_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("recorded_at 必须带时区（UTC 毫秒）")
        return value.astimezone(UTC)

    @field_serializer("recorded_at")
    def _serialize_recorded_at(self, value: datetime) -> str:
        return format_recorded_at(value)

    @field_validator("payload")
    @classmethod
    def _validate_payload(cls, value: dict[str, Any], info: ValidationInfo) -> dict[str, Any]:
        record_type = info.data.get("type")
        if record_type is None:
            raise ValueError("payload 校验前必须已确定 type")
        return validate_rollout_payload(record_type, value)

    @model_validator(mode="after")
    def _check_turn_scope(self) -> RolloutRecord:
        if self.type in _THREAD_LEVEL_TYPES:
            if self.turn_id is not None:
                raise ValueError(f"{self.type} 是 thread 级记录，不得携带 turn_id")
        elif self.turn_id is None:
            raise ValueError(f"{self.type} 必须携带 turn_id")
        return self


def validate_rollout_payload(record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """按记录类型严格校验 payload；未知类型直接拒绝（§5.2 Phase 0-A）。"""
    model = PAYLOAD_MODELS.get(record_type)
    if model is None:
        raise RolloutContractError(f"未知持久化记录类型: {record_type}")
    try:
        validated = model.model_validate(payload)
    except Exception as exc:  # pydantic ValidationError → 域级契约错误
        raise RolloutContractError(f"{record_type} payload 非法: {exc}") from exc
    return validated.model_dump(mode="json")


def serialize_rollout_line(record: RolloutRecord) -> str:
    """序列化为单行 JSONL：紧凑分隔符、UTF-8 原文、不带换行。"""
    data = record.model_dump(mode="json")
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def parse_rollout_line(line: str) -> RolloutRecord:
    """解析一行 JSONL；非法 JSON、缺字段、未知类型、负 ordinal 一律拒绝。"""
    stripped = line.strip()
    if not stripped:
        raise RolloutContractError("rollout 行不得为空")
    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise RolloutContractError(f"rollout 行不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RolloutContractError("rollout 行必须是 JSON 对象")
    try:
        return RolloutRecord.model_validate(raw)
    except Exception as exc:
        raise RolloutContractError(f"rollout 行不满足契约: {exc}") from exc
