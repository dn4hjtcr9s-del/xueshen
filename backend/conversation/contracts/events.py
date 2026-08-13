"""Turn Event 写入契约（§7.4）。

所有事件追加统一通过 TurnEventWriter：必须在同一数据库事务中锁定目标 Turn 行、
原子 +1 使用 conversation_turns.last_event_sequence、以新值插入事件，
依赖 (turn_id, sequence) 唯一约束防重。禁止 SELECT MAX(sequence)+1，
也禁止 API、Graph Worker 或 Publisher 在进程内各自维护计数器。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from backend.conversation.contracts.api import (
    AnswerCompletedPayload,
    AnswerDeltaPayload,
    CitationAvailablePayload,
    ConversationEventType,
    MemorySubmissionPayload,
    TurnAcceptedPayload,
    TurnCancelledPayload,
    TurnDegradedPayload,
    TurnFailedPayload,
    TurnStartedPayload,
)


class TurnEventWrite(BaseModel):
    """一次事件写入请求（由调用方在已有数据库事务内构造）。"""

    model_config = ConfigDict(extra="forbid")

    turn_id: UUID
    event_type: ConversationEventType
    request_id: str
    run_id: str
    payload: dict[str, object]


#: 各事件类型的合法 payload 校验器（契约级；写入时再序列化为 JSON 持久化）
_PAYLOAD_MODELS: dict[str, type[BaseModel]] = {
    "turn.accepted": TurnAcceptedPayload,
    "turn.started": TurnStartedPayload,
    "answer.delta": AnswerDeltaPayload,
    "citation.available": CitationAvailablePayload,
    "turn.degraded": TurnDegradedPayload,
    "memory.submission": MemorySubmissionPayload,
    "answer.completed": AnswerCompletedPayload,
    "turn.failed": TurnFailedPayload,
    "turn.cancelled": TurnCancelledPayload,
}


def validate_event_payload(event_type: str, payload: dict[str, object]) -> dict[str, object]:
    """校验事件 payload 严格符合固定形状（§17.4.1）。"""
    model = _PAYLOAD_MODELS.get(event_type)
    if model is None:
        raise ValueError(f"未知事件类型: {event_type}")
    return model.model_validate(payload).model_dump(mode="json")


class AnswerDeltaAggregator:
    """answer.delta 小窗口聚合（§7.4：默认 64 字符或 100ms 任一条件即 flush）。

    只负责首次持久化时的聚合；重放按原事件逐条返回（附录 A.6），不重新聚合。
    修复（评审 C2）：时间窗口检查由调用方传入单调时钟（monotonic_ms），
    should_flush_by_time 不再依赖永不更新的 _last_flush_ms。
    """

    def __init__(self, *, batch_chars: int = 64, batch_ms: float = 100.0) -> None:
        self._batch_chars = batch_chars
        self._batch_ms = batch_ms
        self._buffer = ""
        self._last_flush_ms: float | None = None

    def append(self, text: str) -> str | None:
        """追加文本；达到字符条件时返回待持久化的聚合块，否则返回 None。"""
        self._buffer += text
        if self._buffer and len(self._buffer) >= self._batch_chars:
            return self.flush()
        return None

    def should_flush_by_time(self, now_ms: float) -> bool:
        """时间条件（§7.4：100ms 任一条件即 flush）。调用方每次 poll 传入单调时钟。"""
        if not self._buffer:
            return False
        if self._last_flush_ms is None:
            self._last_flush_ms = now_ms
            return False
        if now_ms - self._last_flush_ms >= self._batch_ms:
            self._last_flush_ms = now_ms
            return True
        return False

    def flush(self) -> str | None:
        if not self._buffer:
            return None
        chunk = self._buffer
        self._buffer = ""
        return chunk
