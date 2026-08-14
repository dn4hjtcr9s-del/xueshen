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
