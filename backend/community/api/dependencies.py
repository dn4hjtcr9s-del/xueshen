"""Community API 依赖（方案 §4.3 / §8.2 / §9.3，v1.6 冻结 + v3.9 增补）。

- CommunityRuntime：composition root 装配（DB + 只读服务 + 公开资料 adapter）；
- 游标依赖：公共列表/回复分页 bind_principal=False（D13），通知游标绑用户；
- 限流依赖：§9.3 bucket 表 + user/IP 双 bucket + Retry-After（D18/D19/D46c）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Depends, Request

from backend.auth.context import AuthContext
from backend.community.contracts.errors import (
    AdminRequiredError,
    CommunityCursorInvalidError,
    CommunityRateLimitedError,
)
from backend.community.persistence.database import CommunityDatabase
from backend.community.services.post_service import PostReadService
from backend.settings import Settings
from backend.shared.auth_context import get_auth_context, get_optional_auth_context
from backend.shared.client_ip import client_ip as resolve_client_ip
from backend.shared.cursor import CursorError
from backend.shared.cursor import resolve_cursor as shared_resolve_cursor
from backend.shared.ratelimit import FixedWindowRateLimiter, retry_after_seconds

if TYPE_CHECKING:
    from backend.community.services.attachment_service import AttachmentUploadService
    from backend.community.services.board_application_service import BoardApplicationService
    from backend.community.services.post_command_service import PostCommandService
    from backend.community.services.public_user_profile_reader import PublicUserProfileReader
    from backend.community.services.reply_service import ReplyService
    from backend.community.storage.base import StorageBackend


@dataclass
class CommunityRuntime:
    """Community 域运行时依赖。

    PR-B：post_service 只读；PR-C 追加 post_command_service / reply_service /
    profile_reader_factory。V2 追加 attachment_upload_service /
    board_application_service / storage。
    """

    settings: Settings
    database: CommunityDatabase
    post_service: PostReadService
    post_command_service: PostCommandService | None = None
    reply_service: ReplyService | None = None
    profile_reader_factory: Callable[[], PublicUserProfileReader] | None = None
    attachment_upload_service: AttachmentUploadService | None = None
    board_application_service: BoardApplicationService | None = None
    storage: StorageBackend | None = None


def get_community_runtime(request: Request) -> CommunityRuntime:
    runtime: CommunityRuntime | None = getattr(request.app.state, "community_runtime", None)
    if runtime is None:
        raise RuntimeError("Community 运行时尚未初始化")
    return runtime


def get_post_service(request: Request) -> PostReadService:
    return get_community_runtime(request).post_service


def get_post_command_service(request: Request) -> PostCommandService:
    service = get_community_runtime(request).post_command_service
    if service is None:
        raise RuntimeError("Community 写服务尚未装配")
    return service


def get_reply_service(request: Request) -> ReplyService:
    service = get_community_runtime(request).reply_service
    if service is None:
        raise RuntimeError("Community 写服务尚未装配")
    return service


def get_profile_reader(request: Request) -> PublicUserProfileReader:
    """公开资料 adapter（§9.2/D9）：从 runtime 延迟工厂构建。"""
    factory = get_community_runtime(request).profile_reader_factory
    if factory is None:
        raise RuntimeError("Community 公开资料 adapter 尚未装配")
    return factory()


def get_attachment_upload_service(request: Request) -> AttachmentUploadService:
    service = get_community_runtime(request).attachment_upload_service
    if service is None:
        raise RuntimeError("Community 附件上传服务尚未装配")
    return service


def get_application_service(request: Request) -> BoardApplicationService:
    service = get_community_runtime(request).board_application_service
    if service is None:
        raise RuntimeError("Community 建吧申请服务尚未装配")
    return service


def get_storage(request: Request) -> StorageBackend:
    storage = get_community_runtime(request).storage
    if storage is None:
        raise RuntimeError("Community 存储后端尚未装配")
    return storage


def require_community_admin(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
    """社区域管理员校验：非管理员 → 403 ADMIN_REQUIRED。

    区别于 shared require 的 AUTH_FORBIDDEN。
    """
    from backend.settings import get_settings

    settings = get_settings()
    if auth.user_id not in settings.community_admin_user_ids_set:
        raise AdminRequiredError("需要社区管理员权限")
    return auth


# ---------------------------------------------------------------------------
# 游标依赖（§8.2/D13/D39）
# ---------------------------------------------------------------------------


def _community_cursor_error(exc: CursorError) -> CommunityCursorInvalidError:
    # 游标过期/非法统一返回 COMMUNITY_CURSOR_INVALID（§8.7：不区分原因）
    return CommunityCursorInvalidError(str(exc))


def resolve_public_cursor(
    request: Request,
    route: str,
    token: str | None,
    *,
    filters: dict[str, Any],
) -> dict[str, Any] | None:
    """公共游标（D13：bind_principal=False）：帖子列表与详情回复分页。"""
    if token is None:
        return None
    settings = request.app.state.settings
    try:
        return shared_resolve_cursor(
            settings,
            token,
            route=route,
            user_id=UUID(int=0),  # 不绑定 principal，user_id 仅占位
            filters=filters,
            bind_principal=False,
        )
    except CursorError as exc:
        raise _community_cursor_error(exc) from exc


def resolve_private_cursor(
    request: Request,
    route: str,
    token: str | None,
    *,
    user_id: UUID,
    filters: dict[str, Any],
) -> dict[str, Any] | None:
    """私有游标（绑定 principal）：通知列表（§8.6/D13）。"""
    if token is None:
        return None
    settings = request.app.state.settings
    try:
        return shared_resolve_cursor(
            settings,
            token,
            route=route,
            user_id=user_id,
            filters=filters,
            bind_principal=True,
        )
    except CursorError as exc:
        raise _community_cursor_error(exc) from exc


# ---------------------------------------------------------------------------
# 限流（§9.3 / D18 / D19 / D46c）
# ---------------------------------------------------------------------------

#: §9.3 bucket 表：bucket → (settings 字段, window_seconds)
_RATE_LIMIT_BUCKETS: dict[str, tuple[str, int]] = {
    "community.post.create": ("community_rate_limit_post_per_hour", 3600),
    "community.reply.create.minute": ("community_rate_limit_reply_per_minute", 60),
    "community.reply.create.hour": ("community_rate_limit_reply_per_hour", 3600),
    "community.post.like": ("community_rate_limit_like_per_minute", 60),
    "community.read": ("community_rate_limit_read_per_minute", 60),
    "community.upload": ("community_rate_limit_upload_per_hour", 3600),
    "community.application": ("community_rate_limit_application_per_day", 3600 * 24),
    "community.admin.review": ("community_rate_limit_admin_review_per_hour", 3600),
}


def rate_limit(bucket: str) -> Any:
    """§9.3 限流依赖：按 user_id 和 IP 分别建桶，任一命中即拒绝。

    D46c：匿名 = 仅 IP 桶；无 IP 用固定 ip:unknown 桶。
    """

    async def _dep(
        request: Request,
        auth: AuthContext | None = Depends(get_optional_auth_context),
    ) -> None:
        settings: Settings = request.app.state.settings
        limit_field, window_seconds = _RATE_LIMIT_BUCKETS[bucket]
        limit = int(getattr(settings, limit_field))
        limiter: FixedWindowRateLimiter = request.app.state.rate_limiter
        ip = resolve_client_ip(request, settings.community_trusted_proxy_cidrs)
        if auth is not None:
            if not limiter.hit(bucket, str(auth.user_id), limit, window_seconds):
                raise _rate_limited(window_seconds)
        if ip is not None:
            ip_key = f"ip:{ip}"
        else:
            ip_key = "ip:unknown"
        if not limiter.hit(bucket, ip_key, limit, window_seconds):
            raise _rate_limited(window_seconds)

    return _dep


def _rate_limited(window_seconds: int) -> CommunityRateLimitedError:
    error = CommunityRateLimitedError("请求频率过高，请稍后重试")
    # 429 Retry-After 按当前窗口剩余秒数（§9.3/D19：不是固定 60）
    error.retry_after = retry_after_seconds(window_seconds)  # type: ignore[attr-defined]
    return error
