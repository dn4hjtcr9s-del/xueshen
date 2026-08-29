"""Community 域公开错误模型与错误码（方案 §8.7，v1.6 冻结）。

与 Conversation 同模式：信封复用共享 PublicError
（backend/memory/contracts/errors.py），HTTP 输出由 app.py 的
exception handler 统一转换。Memory 投递状态不是公共错误码（§8.7）。
"""

from __future__ import annotations

#: Community 域错误码全集（§8.7 冻结）
COMMUNITY_ERROR_CODES: frozenset[str] = frozenset(
    {
        "COMMUNITY_NOT_FOUND",
        "COMMUNITY_BOARD_DISABLED",
        "COMMUNITY_POST_CLOSED",
        "COMMUNITY_CONTENT_INVALID",
        "COMMUNITY_IDEMPOTENCY_CONFLICT",
        "COMMUNITY_CURSOR_INVALID",
        "COMMUNITY_RATE_LIMITED",
        "UPLOAD_TOO_LARGE",
        "UPLOAD_INVALID_TYPE",
        "UPLOAD_BOMB_REJECTED",
        "COMMUNITY_UPLOAD_FAILED",
        "ATTACHMENT_LIMIT_EXCEEDED",
        "ATTACHMENT_FORBIDDEN",
        "ATTACHMENT_CONFLICT",
        "APPLICATION_DUPLICATE_PENDING",
        "APPLICATION_ALREADY_REVIEWED",
        "BOARD_NAME_CONFLICT",
        "BOARD_SLUG_RESERVED",
        "REJECT_REASON_INVALID",
        "ADMIN_REQUIRED",
    }
)


class CommunityError(Exception):
    """业务错误基类。http_status 与公开错误码一一对应（§8.7）。"""

    code: str = "INTERNAL_ERROR"
    http_status: int = 500
    retryable: bool = False

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


class CommunityNotFoundError(CommunityError):
    """帖子/回复/板块不存在或无权访问（§8.7：404，不泄露对象状态）。"""

    code = "COMMUNITY_NOT_FOUND"
    http_status = 404


class CommunityBoardDisabledError(CommunityError):
    """板块不可发帖（status=hidden，§8.7：409）。"""

    code = "COMMUNITY_BOARD_DISABLED"
    http_status = 409


class CommunityPostClosedError(CommunityError):
    """帖子已关闭/已删除，不能回复或修改解决状态（§8.7/D31：409）。

    D31：deleted 帖子（含其墓碑）的回复/解决操作统一返回本错误；
    hidden 帖子仍返回 COMMUNITY_NOT_FOUND。
    """

    code = "COMMUNITY_POST_CLOSED"
    http_status = 409


class CommunityContentInvalidError(CommunityError):
    """标题/正文为空、过长或含非法格式（§8.7/D37：422）。"""

    code = "COMMUNITY_CONTENT_INVALID"
    http_status = 422


class CommunityIdempotencyConflictError(CommunityError):
    """幂等键对应不同请求体（§8.7：422）。"""

    code = "COMMUNITY_IDEMPOTENCY_CONFLICT"
    http_status = 422


class CommunityCursorInvalidError(CommunityError):
    """游标签名、绑定或有效期非法（§8.7：422）。"""

    code = "COMMUNITY_CURSOR_INVALID"
    http_status = 422


class CommunityRateLimitedError(CommunityError):
    """超过发帖/回复/点赞频率限制（§8.7：429）。"""

    code = "COMMUNITY_RATE_LIMITED"
    http_status = 429
    retryable = True


class UploadTooLargeError(CommunityError):
    """单图超过尺寸上限（§7.10：422）。"""

    code = "UPLOAD_TOO_LARGE"
    http_status = 422


class UploadInvalidTypeError(CommunityError):
    """图片类型不合法或无法解码（§7.10：422）。"""

    code = "UPLOAD_INVALID_TYPE"
    http_status = 422


class UploadBombRejectedError(CommunityError):
    """图片像素超过阈值（§7.10：422）。"""

    code = "UPLOAD_BOMB_REJECTED"
    http_status = 422


class CommunityUploadFailedError(CommunityError):
    """对象存储上传/删除失败（§7.9/§8：502，实例级 retryable）。"""

    code = "COMMUNITY_UPLOAD_FAILED"
    http_status = 502

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message, field=field)
        self.retryable = retryable


class AttachmentLimitExceededError(CommunityError):
    """每帖附件数量超过上限（§8：422）。"""

    code = "ATTACHMENT_LIMIT_EXCEEDED"
    http_status = 422


class AttachmentForbiddenError(CommunityError):
    """附件不属于当前用户（§7.14：403）。"""

    code = "ATTACHMENT_FORBIDDEN"
    http_status = 403


class AttachmentConflictError(CommunityError):
    """附件已绑定/不存在/已被清理（§7.14：409）。"""

    code = "ATTACHMENT_CONFLICT"
    http_status = 409


class ApplicationDuplicatePendingError(CommunityError):
    """用户已有 pending 建吧申请（§7.4：409）。"""

    code = "APPLICATION_DUPLICATE_PENDING"
    http_status = 409


class ApplicationAlreadyReviewedError(CommunityError):
    """申请已被审核过（§7.4：409）。"""

    code = "APPLICATION_ALREADY_REVIEWED"
    http_status = 409


class BoardNameConflictError(CommunityError):
    """吧名/slug 已被占用（§7.4：409）。"""

    code = "BOARD_NAME_CONFLICT"
    http_status = 409


class BoardSlugReservedError(CommunityError):
    """slug 为保留字（§7.4：422）。"""

    code = "BOARD_SLUG_RESERVED"
    http_status = 422


class RejectReasonInvalidError(CommunityError):
    """拒绝理由不合法（§7.4：422）。"""

    code = "REJECT_REASON_INVALID"
    http_status = 422


class AdminRequiredError(CommunityError):
    """非管理员访问管理接口（§8：403）。"""

    code = "ADMIN_REQUIRED"
    http_status = 403
