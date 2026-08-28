"""Conversation 域公开错误模型与错误码（方案 §17.6 / 附录 A.8）。

与 Memory 域共享 PublicError 信封（backend/memory/contracts/errors.py），
但错误码与异常层级独立命名空间：ConversationError → 具体异常，
backend/app.py 注册 ConversationError 的 exception handler 统一输出。
"""

from __future__ import annotations

# 第一版 Conversation 域错误码全集（方案 §17.6）
CONVERSATION_ERROR_CODES: frozenset[str] = frozenset(
    {
        "CONVERSATION_NOT_FOUND",
        "TURN_NOT_FOUND",
        "TURN_ALREADY_RUNNING",
        "THREAD_VERSION_CONFLICT",
        "TURN_ALREADY_COMPLETED",
        "TURN_CANCELLED",
        "MODEL_UNAVAILABLE",
        "RETRIEVAL_UNAVAILABLE",
        "MEMORY_UNAVAILABLE",
        "ANSWER_VALIDATION_FAILED",
        "REQUEST_IDEMPOTENCY_CONFLICT",
        "EVENT_REPLAY_EXPIRED",
        "KNOWLEDGE_SUMMARY_NOT_FOUND",
        "KNOWLEDGE_SUMMARY_GENERATION_NOT_FOUND",
        "KNOWLEDGE_SUMMARY_SOURCE_NOT_FOUND",
        "KNOWLEDGE_SUMMARY_VERSION_CONFLICT",
        "KNOWLEDGE_SUMMARY_MERGE_CONFLICT",
        "KNOWLEDGE_SUMMARY_GENERATION_NOT_READY",
        "KNOWLEDGE_SUMMARY_REQUEST_IDEMPOTENCY_CONFLICT",
        "KNOWLEDGE_SUMMARY_INVALID_CONTENT",
        "KNOWLEDGE_SUMMARY_SOURCE_CHANGED",
        "KNOWLEDGE_SUMMARY_RATE_LIMITED",
        "KNOWLEDGE_SUMMARY_INVALID_CURSOR",
        "KNOWLEDGE_SUMMARY_SOURCE_SUPPRESSED",
        "KNOWLEDGE_SUMMARY_REVIEW_NOT_FOUND",
    }
)


class ConversationError(Exception):
    """业务错误基类。http_status 与公开错误码一一对应。

    信封复用共享 PublicError（backend/memory/contracts/errors.py，附录 A.4）；
    本域只定义异常层级与错误码，HTTP 输出由 app.py 的 handler 统一转换。
    """

    code: str = "INTERNAL_ERROR"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class ConversationNotFoundError(ConversationError):
    code = "CONVERSATION_NOT_FOUND"
    http_status = 404


class TurnNotFoundError(ConversationError):
    code = "TURN_NOT_FOUND"
    http_status = 404


class TurnAlreadyRunningError(ConversationError):
    code = "TURN_ALREADY_RUNNING"
    http_status = 409
    retryable = True


class ThreadVersionConflictError(ConversationError):
    """expected_thread_version 与当前版本不符（§17.6 / 附录 A.4）。

    响应顶层 error 对象带 current_version 字段。
    """

    code = "THREAD_VERSION_CONFLICT"
    http_status = 409

    def __init__(self, message: str, *, field: str | None = None, current_version: int) -> None:
        super().__init__(message, field=field)
        self.current_version = current_version


class TurnAlreadyCompletedError(ConversationError):
    code = "TURN_ALREADY_COMPLETED"
    http_status = 409


class TurnCancelledError(ConversationError):
    code = "TURN_CANCELLED"
    http_status = 409


class ModelUnavailableError(ConversationError):
    code = "MODEL_UNAVAILABLE"
    http_status = 503
    retryable = True


class StructuredOutputError(ModelUnavailableError):
    """结构化输出无法可靠完成，保留不含正文的诊断元数据。"""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        attempts: int,
        response_status: str | None = None,
        incomplete_reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.attempts = attempts
        self.response_status = response_status
        self.incomplete_reason = incomplete_reason


class RetrievalUnavailableError(ConversationError):
    code = "RETRIEVAL_UNAVAILABLE"
    http_status = 503
    retryable = True


class MemoryUnavailableError(ConversationError):
    """Memory 不可用（§16.2 / 第三轮必改 4）。

    source_http_status 保留底层 Memory API 的 HTTP 状态：401/403/4xx 契约错误
    必须使 Turn 失败（不静默降级），5xx/超时/网络才按 unavailable 快照继续。
    """

    code = "MEMORY_UNAVAILABLE"
    http_status = 503
    retryable = True

    def __init__(
        self, message: str, *, field: str | None = None, source_http_status: int | None = None
    ) -> None:
        super().__init__(message, field=field)
        self.source_http_status = source_http_status


class AnswerValidationFailedError(ConversationError):
    code = "ANSWER_VALIDATION_FAILED"
    http_status = 422


class RequestIdempotencyConflictError(ConversationError):
    """client_request_id 已存在但归属/内容冲突（§17.2 幂等语义）。"""

    code = "REQUEST_IDEMPOTENCY_CONFLICT"
    http_status = 422


class EventReplayExpiredError(ConversationError):
    """Last-Event-ID 早于该 Turn 最早保留事件（§1.5 R1 / §17.5）。

    HTTP 410、retryable=false；前端重新拉取 Turn/Thread 后再次订阅。
    """

    code = "EVENT_REPLAY_EXPIRED"
    http_status = 410


# ---------------------------------------------------------------------------
# KnowledgeSummary（知识总结方案 §16）
# ---------------------------------------------------------------------------


class KnowledgeSummaryError(ConversationError):
    """知识总结业务错误基类，保持在 Conversation 错误边界内。"""


class KnowledgeSummaryNotFoundError(KnowledgeSummaryError):
    code = "KNOWLEDGE_SUMMARY_NOT_FOUND"
    http_status = 404


class KnowledgeSummaryGenerationNotFoundError(KnowledgeSummaryError):
    code = "KNOWLEDGE_SUMMARY_GENERATION_NOT_FOUND"
    http_status = 404


class KnowledgeSummarySourceNotFoundError(KnowledgeSummaryError):
    code = "KNOWLEDGE_SUMMARY_SOURCE_NOT_FOUND"
    http_status = 404


class KnowledgeSummaryVersionConflictError(KnowledgeSummaryError):
    """总结 PATCH/DELETE 的乐观并发版本冲突。"""

    code = "KNOWLEDGE_SUMMARY_VERSION_CONFLICT"
    http_status = 409

    def __init__(self, message: str, *, current_version: int, field: str | None = None) -> None:
        super().__init__(message, field=field)
        self.current_version = current_version


class KnowledgeSummaryMergeConflictError(KnowledgeSummaryError):
    code = "KNOWLEDGE_SUMMARY_MERGE_CONFLICT"
    http_status = 409


class KnowledgeSummaryGenerationNotReadyError(KnowledgeSummaryError):
    code = "KNOWLEDGE_SUMMARY_GENERATION_NOT_READY"
    http_status = 409
    retryable = True


class KnowledgeSummaryRequestIdempotencyConflictError(KnowledgeSummaryError):
    code = "KNOWLEDGE_SUMMARY_REQUEST_IDEMPOTENCY_CONFLICT"
    http_status = 422


class KnowledgeSummaryInvalidContentError(KnowledgeSummaryError):
    code = "KNOWLEDGE_SUMMARY_INVALID_CONTENT"
    http_status = 422


class KnowledgeSummarySourceChangedError(KnowledgeSummaryError):
    code = "KNOWLEDGE_SUMMARY_SOURCE_CHANGED"
    http_status = 422


class KnowledgeSummaryRateLimitedError(KnowledgeSummaryError):
    code = "KNOWLEDGE_SUMMARY_RATE_LIMITED"
    http_status = 429
    retryable = True

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class KnowledgeSummaryInvalidCursorError(KnowledgeSummaryError):
    code = "KNOWLEDGE_SUMMARY_INVALID_CURSOR"
    http_status = 422


class KnowledgeSummarySourceSuppressedError(KnowledgeSummaryError):
    code = "KNOWLEDGE_SUMMARY_SOURCE_SUPPRESSED"
    http_status = 409


class KnowledgeSummaryReviewNotFoundError(KnowledgeSummaryError):
    code = "KNOWLEDGE_SUMMARY_REVIEW_NOT_FOUND"
    http_status = 404
