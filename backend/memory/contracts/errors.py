"""公开错误模型与错误码（规格 §7.3）。

异常层级：MemoryError → 具体错误，Gateway 统一转换为 PublicError HTTP 响应。
"""

from __future__ import annotations

from pydantic import BaseModel

# 第一版错误码全集（§7.3）
ERROR_CODES: frozenset[str] = frozenset(
    {
        "AUTH_REQUIRED",
        "AUTH_FORBIDDEN",
        "INVALID_PAYLOAD",
        "REQUEST_EXTRA_FIELD",
        "INVALID_IDEMPOTENCY_KEY",
        "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD",
        "MEMORY_NOT_FOUND",
        "MEMORY_VERSION_CONFLICT",
        "MEMORY_DELETED",
        "MEMORY_RESTORE_EXPIRED",
        "CANDIDATE_NOT_FOUND",
        "CANDIDATE_ALREADY_REVIEWED",
        "GRAPH_NODE_NOT_FOUND",
        "GRAPH_STATUS_NOT_USER_SETTABLE",
        "GRAPH_STATE_VERSION_CONFLICT",
        "GRAPH_STATE_VERSION_REQUIRED",
        "OPERATION_CANCEL_NOT_ALLOWED",
        "OPERATION_NOT_FOUND",
        "NOTIFICATION_NOT_FOUND",
        "IDENTITY_MAPPING_NOT_FOUND",
        "ACCOUNT_PURGE_ALREADY_RUNNING",
        "SOURCE_TOO_LARGE",
        "CURSOR_INVALID",
        "CURSOR_EXPIRED",
        "SOURCE_NOT_FOUND",
        "SOURCE_ACCESS_DENIED",
        "SOURCE_DELETED",
        "OPENAI_TIMEOUT",
        "OPENAI_RATE_LIMITED",
        "OPENAI_SCHEMA_INVALID",
        "STORAGE_UNAVAILABLE",
        "DATABASE_UNAVAILABLE",
        "OPERATION_NEEDS_REVIEW",
        "OPERATION_DEAD_LETTER",
        "RATE_LIMITED",
        "INTERNAL_ERROR",
    }
)


class PublicError(BaseModel):
    code: str
    message: str
    retryable: bool
    field: str | None = None
    trace_id: str


class MemoryError(Exception):
    """业务错误基类。http_status 与公开错误码一一对应。"""

    code: str = "INTERNAL_ERROR"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class InvalidPayloadError(MemoryError):
    code = "INVALID_PAYLOAD"
    http_status = 422


class RequestExtraFieldError(MemoryError):
    """公开请求携带契约外字段（§6.4 / §19.5）。"""

    code = "REQUEST_EXTRA_FIELD"
    http_status = 422


class InvalidIdempotencyKeyError(MemoryError):
    code = "INVALID_IDEMPOTENCY_KEY"
    http_status = 422


class IdempotencyKeyReusedError(MemoryError):
    code = "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"
    http_status = 422


class MemoryNotFoundError(MemoryError):
    code = "MEMORY_NOT_FOUND"
    http_status = 404


class MemoryVersionConflictError(MemoryError):
    code = "MEMORY_VERSION_CONFLICT"
    http_status = 409


class MemoryDeletedError(MemoryError):
    code = "MEMORY_DELETED"
    http_status = 422


class MemoryRestoreExpiredError(MemoryError):
    code = "MEMORY_RESTORE_EXPIRED"
    http_status = 422


class CandidateNotFoundError(MemoryError):
    code = "CANDIDATE_NOT_FOUND"
    http_status = 404


class CandidateAlreadyReviewedError(MemoryError):
    code = "CANDIDATE_ALREADY_REVIEWED"
    http_status = 409


class GraphNodeNotFoundError(MemoryError):
    code = "GRAPH_NODE_NOT_FOUND"
    http_status = 404


class GraphStatusNotUserSettableError(MemoryError):
    code = "GRAPH_STATUS_NOT_USER_SETTABLE"
    http_status = 422


class GraphStateVersionConflictError(MemoryError):
    code = "GRAPH_STATE_VERSION_CONFLICT"
    http_status = 409


class GraphStateVersionRequiredError(MemoryError):
    code = "GRAPH_STATE_VERSION_REQUIRED"
    http_status = 422


class OperationCancelNotAllowedError(MemoryError):
    code = "OPERATION_CANCEL_NOT_ALLOWED"
    http_status = 409


class OperationNotFoundError(MemoryError):
    """operation 不存在或不属于当前用户（§7.3 未列 operation 专用码，见施工报告）。"""

    code = "OPERATION_NOT_FOUND"
    http_status = 404


class NotificationNotFoundError(MemoryError):
    """通知不存在或不属于当前用户（§7.3 未列通知专用码，见施工报告）。"""

    code = "NOTIFICATION_NOT_FOUND"
    http_status = 404


class IdentityMappingNotFoundError(MemoryError):
    code = "IDENTITY_MAPPING_NOT_FOUND"
    http_status = 404


class AccountPurgeAlreadyRunningError(MemoryError):
    code = "ACCOUNT_PURGE_ALREADY_RUNNING"
    http_status = 409


class SourceTooLargeError(MemoryError):
    code = "SOURCE_TOO_LARGE"
    http_status = 422


class CursorInvalidError(MemoryError):
    code = "CURSOR_INVALID"
    http_status = 422


class CursorExpiredError(MemoryError):
    code = "CURSOR_EXPIRED"
    http_status = 422


class SourceNotFoundError(MemoryError):
    code = "SOURCE_NOT_FOUND"
    http_status = 404


class SourceAccessDeniedError(MemoryError):
    code = "SOURCE_ACCESS_DENIED"
    http_status = 403


class SourceDeletedError(MemoryError):
    code = "SOURCE_DELETED"
    http_status = 422


class OpenAITimeoutError(MemoryError):
    code = "OPENAI_TIMEOUT"
    http_status = 503
    retryable = True


class OpenAIRateLimitedError(MemoryError):
    code = "OPENAI_RATE_LIMITED"
    http_status = 503
    retryable = True


class OpenAISchemaInvalidError(MemoryError):
    code = "OPENAI_SCHEMA_INVALID"
    http_status = 503


class StorageUnavailableError(MemoryError):
    code = "STORAGE_UNAVAILABLE"
    http_status = 503
    retryable = True


class DatabaseUnavailableError(MemoryError):
    code = "DATABASE_UNAVAILABLE"
    http_status = 503
    retryable = True


class OperationNeedsReviewError(MemoryError):
    code = "OPERATION_NEEDS_REVIEW"
    http_status = 422


class OperationDeadLetterError(MemoryError):
    code = "OPERATION_DEAD_LETTER"
    http_status = 422


class RateLimitedError(MemoryError):
    code = "RATE_LIMITED"
    http_status = 429
    retryable = True
