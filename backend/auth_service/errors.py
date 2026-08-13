"""认证服务公开错误（方案 §4.2）：统一 AuthServiceError → PublicError 响应体。"""

from __future__ import annotations


class AuthServiceError(Exception):
    """认证服务错误。code 为公开错误码，与方案 §4.2 表格一一对应。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int,
        retryable: bool = False,
        field: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.field = field
        self.retry_after = retry_after


def username_taken() -> AuthServiceError:
    return AuthServiceError(
        "AUTH_USERNAME_TAKEN", "用户名已存在", http_status=409, field="username"
    )


def email_taken() -> AuthServiceError:
    return AuthServiceError("AUTH_EMAIL_TAKEN", "邮箱已存在", http_status=409, field="email")


def invalid_username(message: str) -> AuthServiceError:
    return AuthServiceError("AUTH_INVALID_USERNAME", message, http_status=422, field="username")


def invalid_email(message: str) -> AuthServiceError:
    return AuthServiceError("AUTH_INVALID_EMAIL", message, http_status=422, field="email")


def weak_password(message: str) -> AuthServiceError:
    return AuthServiceError("AUTH_WEAK_PASSWORD", message, http_status=422, field="password")


def invalid_credentials() -> AuthServiceError:
    return AuthServiceError(
        "AUTH_INVALID_CREDENTIALS",
        "账号或密码错误",
        http_status=401,
    )


def session_invalid() -> AuthServiceError:
    return AuthServiceError(
        "AUTH_SESSION_INVALID", "登录会话无效或已过期，请重新登录", http_status=401
    )


def mapping_pending() -> AuthServiceError:
    return AuthServiceError(
        "AUTH_MAPPING_PENDING",
        "身份映射暂时无法建立，请稍后重试",
        http_status=503,
        retryable=True,
    )


def auth_db_unavailable() -> AuthServiceError:
    return AuthServiceError(
        "AUTH_DB_UNAVAILABLE",
        "认证数据库暂不可用，请稍后重试",
        http_status=503,
        retryable=True,
    )


def rate_limited(retry_after: int) -> AuthServiceError:
    return AuthServiceError(
        "RATE_LIMITED",
        "请求过于频繁，请稍后重试",
        http_status=429,
        retryable=True,
        retry_after=retry_after,
    )
