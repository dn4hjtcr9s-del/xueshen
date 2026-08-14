"""Community API 依赖（方案 §4.3 / §8.2 / §9.3，v1.6 冻结）。

- CommunityRuntime：composition root 装配（DB + 只读服务 + 公开资料 adapter）；
- 游标依赖：公共列表/回复分页 bind_principal=False（D13），通知游标绑用户；
- 限流依赖：§9.3 bucket 表 + user/IP 双 bucket + Retry-After（D18/D19）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import Depends, Request

from backend.auth.context import AuthContext
from backend.community.contracts.errors import (
    CommunityCursorInvalidError,
    CommunityRateLimitedError,
)
from backend.community.persistence.database import CommunityDatabase
from backend.community.services.post_service import PostReadService
from backend.settings import Settings
from backend.shared.auth_context import get_auth_context
from backend.shared.client_ip import client_ip as resolve_client_ip
from backend.shared.cursor import CursorError
from backend.shared.cursor import resolve_cursor as shared_resolve_cursor
from backend.shared.ratelimit import FixedWindowRateLimiter, retry_after_seconds

if TYPE_CHECKING:
    from backend.community.services.post_command_service import PostCommandService
    from backend.community.services.public_user_profile_reader import PublicUserProfileReader
    from backend.community.services.reply_service import ReplyService


@dataclass
class CommunityRuntime:
    """Community 域运行时依赖。

    PR-B：post_service 只读；PR-C 追加 post_command_service / reply_service /
    profile_reader_factory。写路径依赖 Auth 库 session（公开资料 adapter），
    而 auth_runtime 在 startup 才构建，故 profile reader 使用延迟工厂
    （闭包读取 app.state.auth_runtime，请求到达时已就绪）。
    """

    settings: Settings
    database: CommunityDatabase
    post_service: PostReadService
    post_command_service: PostCommandService | None = None
    reply_service: ReplyService | None = None
    profile_reader_factory: Callable[[], PublicUserProfileReader] | None = None


def get_community_runtime(request: Request) -> CommunityRuntime:
    runtime: CommunityRuntime | None = getattr(request.app.state, "community_runtime", None)
    if runtime is None:
        # 未装配 Community 时路由不会挂载（D25）；此处兜底防御
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
# 限流（§9.3 / D18 / D19）
# ---------------------------------------------------------------------------

#: §9.3 bucket 表：bucket → (settings 字段, window_seconds)
_RATE_LIMIT_BUCKETS: dict[str, tuple[str, int]] = {
    "community.post.create": ("community_rate_limit_post_per_hour", 3600),
    "community.reply.create.minute": ("community_rate_limit_reply_per_minute", 60),
    "community.reply.create.hour": ("community_rate_limit_reply_per_hour", 3600),
    "community.post.like": ("community_rate_limit_like_per_minute", 60),
    "community.read": ("community_rate_limit_read_per_minute", 60),
}


def rate_limit(bucket: str) -> Any:
    """§9.3 限流依赖：按 user_id 和 IP 分别建桶，任一命中即拒绝。

    IP 解析使用共享可信代理解析器（D18）；request.client 缺失时跳过 IP 桶
    仅保留 user_id 桶并记录（禁止 unknown 全局桶）。
    """

    async def _dep(
        request: Request,
        auth: AuthContext = Depends(get_auth_context),
    ) -> None:
        settings: Settings = request.app.state.settings
        limit_field, window_seconds = _RATE_LIMIT_BUCKETS[bucket]
        limit = int(getattr(settings, limit_field))
        limiter: FixedWindowRateLimiter = request.app.state.rate_limiter
        ip = resolve_client_ip(request, settings.community_trusted_proxy_cidrs)
        if not limiter.hit(bucket, str(auth.user_id), limit, window_seconds):
            raise _rate_limited(window_seconds)
        if ip is not None and not limiter.hit(bucket, f"ip:{ip}", limit, window_seconds):
            raise _rate_limited(window_seconds)

    return _dep


def _rate_limited(window_seconds: int) -> CommunityRateLimitedError:
    error = CommunityRateLimitedError("请求频率过高，请稍后重试")
    # 429 Retry-After 按当前窗口剩余秒数（§9.3/D19：不是固定 60）
    error.retry_after = retry_after_seconds(window_seconds)  # type: ignore[attr-defined]
    return error
