"""Community 公共 REST API（方案 §8，PR-B 只读 + PR-C 写纵切）。

前缀 /api/v1/community；全部端点要求登录用户（§8）。
游标规则（§8.2/D13）：公共列表/回复分页不绑定 principal；回复游标绑定
具体 post_id（D39）；通知游标绑定当前用户（私有游标）。
限流（§9.3/D41）：community.read 覆盖列表/详情/回复分页/通知读取；
写路径按 bucket 表（发帖小时级、回复分钟+小时双桶、点赞分钟）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict

from backend.auth.context import AuthContext
from backend.community.api.dependencies import (
    get_post_command_service,
    get_post_service,
    get_reply_service,
    rate_limit,
    resolve_private_cursor,
    resolve_public_cursor,
)
from backend.community.contracts.api import (
    CommunityBoard,
    CommunityNotification,
    CommunityNotificationPage,
    CommunityPostDetail,
    CommunityPostDetailResponse,
    CommunityPostSummary,
    CommunityReplyView,
    CreatePostRequest,
    CreateReplyRequest,
    ResolveRequest,
)
from backend.community.contracts.errors import (
    CommunityContentInvalidError,
    CommunityCursorInvalidError,
)
from backend.community.services.post_service import PostReadService
from backend.shared.auth_context import get_auth_context
from backend.shared.cursor import issue_cursor

router = APIRouter(prefix="/api/v1/community", tags=["community"])


class PostListResponse(BaseModel):
    """帖子列表分页响应（§8.2/D45 信封）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[CommunityPostSummary]
    next_cursor: str | None
    has_more: bool


class BoardListResponse(BaseModel):
    """板块列表响应（§8.1/D45：无分页仅 {items}）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[CommunityBoard]


#: 帖子列表游标 route 标识（§8.2 绑定项；与通知/回复路由互斥）
_POSTS_ROUTE = "community.posts"
#: 详情回复分页游标 route：绑定具体 post_id 防跨帖子复用（D39）
_REPLIES_ROUTE = "community.posts.detail.replies"


@router.get("/boards", response_model=BoardListResponse)
async def list_boards(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    _rate: None = Depends(rate_limit("community.read")),
) -> BoardListResponse:
    """板块列表（§8.1）：只返回 status=active 板块。"""
    service = get_post_service(request)
    items = await service.list_boards()
    return BoardListResponse(items=items)


@router.get("/posts", response_model=PostListResponse)
async def list_posts(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    board_id: UUID | None = Query(default=None),
    sort: str = Query(default="latest", pattern="^(latest|unanswered)$"),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    _rate: None = Depends(rate_limit("community.read")),
) -> PostListResponse:
    """帖子列表（§8.2）：latest/unanswered + 板块筛选 + keyset 游标。

    游标绑定 route/sort/board filter/最后一条排序 key/expiry（§8.2）；
    公共游标不绑定 principal（D13）。
    """
    filters: dict[str, Any] = {"sort": sort}
    if board_id is not None:
        filters["board_id"] = str(board_id)
    payload = resolve_public_cursor(request, _POSTS_ROUTE, cursor, filters=filters)
    after_key: tuple[Any, ...] | None = None
    if payload is not None:
        sort_key = payload.get("sort_key")
        if not isinstance(sort_key, list) or len(sort_key) != 3:
            raise CommunityCursorInvalidError("游标缺少完整排序键")
        after_key = (bool(sort_key[0]), sort_key[1], UUID(str(sort_key[2])))
    service: PostReadService = get_post_service(request)
    items, next_key, has_more = await service.list_posts(
        viewer_user_id=auth.user_id,
        board_id=board_id,
        sort=sort,
        after_key=after_key,
        limit=limit,
    )
    next_cursor: str | None = None
    if has_more and next_key is not None:
        next_cursor = issue_cursor(
            request.app.state.settings,
            route=_POSTS_ROUTE,
            user_id=auth.user_id,
            filters=filters,
            sort_key=list(next_key),
            bind_principal=False,
        )
    return PostListResponse(items=items, next_cursor=next_cursor, has_more=has_more)


@router.get("/posts/{post_id}", response_model=CommunityPostDetailResponse)
async def get_post_detail(
    request: Request,
    post_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    reply_cursor: str | None = Query(default=None),
    reply_limit: int = Query(default=20, ge=1, le=50),
    _rate: None = Depends(rate_limit("community.read")),
) -> CommunityPostDetailResponse:
    """帖子详情 + 一页回复（§8.4）。

    hidden → COMMUNITY_NOT_FOUND（含作者）；deleted → 墓碑契约（§6.6）。
    回复游标绑定具体 post_id（D39），防跨帖子复用。
    """
    route = f"{_REPLIES_ROUTE}:{post_id}"
    filters: dict[str, Any] = {}
    payload = resolve_public_cursor(request, route, reply_cursor, filters=filters)
    reply_after: tuple[Any, ...] | None = None
    if payload is not None:
        sort_key = payload.get("sort_key")
        if not isinstance(sort_key, list) or len(sort_key) != 2:
            raise CommunityCursorInvalidError("游标缺少完整排序键")
        reply_after = (sort_key[0], UUID(str(sort_key[1])))
    service: PostReadService = get_post_service(request)
    response, next_key = await service.get_post_detail(
        viewer_user_id=auth.user_id,
        post_id=post_id,
        reply_after_key=reply_after,
        reply_limit=reply_limit,
    )
    if next_key is not None:
        response.replies.next_cursor = issue_cursor(
            request.app.state.settings,
            route=route,
            user_id=auth.user_id,
            filters=filters,
            sort_key=list(next_key),
            bind_principal=False,
        )
    return response


# ---------------------------------------------------------------------------
# 写路径（§8.3–§8.5，PR-C）：幂等键必填、限流分桶、身份来自认证上下文
# ---------------------------------------------------------------------------


def _require_idempotency_key(idempotency_key: str | None) -> str:
    """§8.3：幂等键缺失/格式非法统一 422（§8.7 无专码，映射 CONTENT_INVALID）。"""
    if not idempotency_key:
        raise CommunityContentInvalidError(
            "缺少或非法的 Idempotency-Key（ASCII 可见字符，1–200）", field="Idempotency-Key"
        )
    return idempotency_key


_IDEMPOTENCY_KEY_RE = r"^[\x21-\x7e]{1,200}$"


@router.post("/posts", response_model=CommunityPostDetail, status_code=201)
async def create_post(
    request: Request,
    payload: CreatePostRequest,
    auth: AuthContext = Depends(get_auth_context),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", pattern=_IDEMPOTENCY_KEY_RE
    ),
    _rate: None = Depends(rate_limit("community.post.create")),
) -> CommunityPostDetail:
    """发帖（§8.3）：user_id 来自认证上下文；幂等重放/冲突语义在 service。"""
    _require_idempotency_key(idempotency_key)
    settings = request.app.state.settings
    service = get_post_command_service(request)
    return await service.create_post(
        user_id=auth.user_id,
        board_id=payload.board_id,
        title=payload.title,
        body=payload.body,
        idempotency_key=idempotency_key or "",
        max_body_chars=settings.community_post_body_max_length,
        idempotency_retention_days=settings.community_idempotency_retention_days,
    )


@router.post("/posts/{post_id}/replies", response_model=CommunityReplyView, status_code=201)
async def create_reply(
    request: Request,
    post_id: UUID,
    payload: CreateReplyRequest,
    auth: AuthContext = Depends(get_auth_context),
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", pattern=_IDEMPOTENCY_KEY_RE
    ),
    _rate: None = Depends(rate_limit("community.reply.create.minute")),
    _rate_hour: None = Depends(rate_limit("community.reply.create.hour")),
) -> CommunityReplyView:
    """回复（§8.4）：分钟 + 小时双窗口限流（§9.3）。"""
    _require_idempotency_key(idempotency_key)
    settings = request.app.state.settings
    service = get_reply_service(request)
    row = await service.create_reply(
        user_id=auth.user_id,
        post_id=post_id,
        body=payload.body,
        idempotency_key=idempotency_key or "",
        max_chars=settings.community_reply_max_length,
        idempotency_retention_days=settings.community_idempotency_retention_days,
    )
    # 刚创建的回复必为 active；不可能是当前解决答案（同一时刻不可能已标记）
    return PostReadService._to_reply(row, auth.user_id, solved=False)


@router.post("/posts/{post_id}/like", status_code=200)
async def like_post(
    request: Request,
    post_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    _rate: None = Depends(rate_limit("community.post.like")),
) -> dict[str, str]:
    """点赞（§8.5）：幂等；deleted/hidden → NOT_FOUND。"""
    await get_post_command_service(request).toggle_like(
        user_id=auth.user_id, post_id=post_id, like=True
    )
    return {"status": "ok"}


@router.delete("/posts/{post_id}/like", status_code=200)
async def unlike_post(
    request: Request,
    post_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    _rate: None = Depends(rate_limit("community.post.like")),
) -> dict[str, str]:
    """取消点赞（§8.5）：与点赞共用限流桶；幂等。"""
    await get_post_command_service(request).toggle_like(
        user_id=auth.user_id, post_id=post_id, like=False
    )
    return {"status": "ok"}


@router.post("/posts/{post_id}/resolve", status_code=200)
async def resolve_post(
    request: Request,
    post_id: UUID,
    payload: ResolveRequest,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, str]:
    """标记解决/取消解决（§8.5：reply_id=null 表示取消）。"""
    await get_post_command_service(request).resolve(
        actor_user_id=auth.user_id, post_id=post_id, reply_id=payload.reply_id
    )
    return {"status": "ok"}


@router.delete("/posts/{post_id}", status_code=200)
async def delete_post(
    request: Request,
    post_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, str]:
    """删除帖子（§11.1）：作者本人；重复删除幂等成功。"""
    await get_post_command_service(request).delete_post(actor_user_id=auth.user_id, post_id=post_id)
    return {"status": "ok"}


@router.delete("/posts/{post_id}/replies/{reply_id}", status_code=200)
async def delete_reply(
    request: Request,
    post_id: UUID,
    reply_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
) -> dict[str, str]:
    """删除回复（§11.1）：作者本人；solved 回复清除解决标记（D34）。"""
    await get_reply_service(request).delete_reply(actor_user_id=auth.user_id, reply_id=reply_id)
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 社区通知（§8.6）：私有游标绑定当前用户（D13）
# ---------------------------------------------------------------------------

_NOTIFICATIONS_ROUTE = "community.notifications"


class ReadAllResponse(BaseModel):
    """两个 read-all 统一响应（§8.6 冻结：{"unread_count": 0}）。"""

    model_config = ConfigDict(extra="forbid")

    unread_count: int


@router.get("/notifications", response_model=CommunityNotificationPage)
async def list_notifications(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
    unread_only: bool = Query(default=False),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
    _rate: None = Depends(rate_limit("community.read")),
) -> CommunityNotificationPage:
    """通知列表（§8.6）：只返回当前用户记录 + 全部未读数。"""
    from backend.community.api.dependencies import get_community_runtime
    from backend.community.persistence import notifications as notifications_repo

    filters: dict[str, Any] = {"unread_only": unread_only}
    payload = resolve_private_cursor(
        request,
        _NOTIFICATIONS_ROUTE,
        cursor,
        user_id=auth.user_id,
        filters=filters,
    )
    after: tuple[Any, UUID] | None = None
    if payload is not None:
        sort_key = payload.get("sort_key")
        if not isinstance(sort_key, list) or len(sort_key) != 2:
            raise CommunityCursorInvalidError("游标缺少完整排序键")
        after = (sort_key[0], UUID(str(sort_key[1])))
    settings = request.app.state.settings
    session_factory = get_community_runtime(request).database.session_factory
    async with session_factory() as session:
        rows = await notifications_repo.list_notifications(
            session,
            user_id=auth.user_id,
            unread_only=unread_only,
            after=after,
            limit=limit,
        )
        unread = await notifications_repo.unread_count(session, user_id=auth.user_id)
    has_more = len(rows) > limit
    items = [
        CommunityNotification(
            notification_id=r["notification_id"],
            event_type=r["event_type"],
            title=r["title"],
            body=r["body"],
            read_at=r["read_at"],
            created_at=r["created_at"],
            post_id=r["post_id"],
            reply_id=r["reply_id"],
        )
        for r in rows[:limit]
    ]
    next_cursor: str | None = None
    if has_more and rows:
        last = rows[limit - 1]
        next_cursor = issue_cursor(
            settings,
            route=_NOTIFICATIONS_ROUTE,
            user_id=auth.user_id,
            filters=filters,
            sort_key=[last["created_at"].isoformat(), str(last["notification_id"])],
            bind_principal=True,
        )
    return CommunityNotificationPage(
        items=items,
        next_cursor=next_cursor,
        has_more=has_more,
        unread_count=unread,
    )


@router.post("/notifications/{notification_id}/read", response_model=ReadAllResponse)
async def mark_notification_read(
    request: Request,
    notification_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
) -> ReadAllResponse:
    """标记单条已读（§8.6：幂等；recipient 条件保证不能跨用户标记）。"""
    from backend.community.api.dependencies import get_community_runtime
    from backend.community.persistence import notifications as notifications_repo

    session_factory = get_community_runtime(request).database.session_factory
    async with session_factory() as session:
        async with session.begin():
            await notifications_repo.mark_read(
                session, user_id=auth.user_id, notification_id=notification_id
            )
            unread = await notifications_repo.unread_count(session, user_id=auth.user_id)
    return ReadAllResponse(unread_count=unread)


@router.post("/notifications/read-all", response_model=ReadAllResponse)
async def read_all_notifications(
    request: Request,
    auth: AuthContext = Depends(get_auth_context),
) -> ReadAllResponse:
    """全部已读（§8.6/D14：只更新当前认证用户的未读记录）。"""
    from backend.community.api.dependencies import get_community_runtime
    from backend.community.persistence import notifications as notifications_repo

    session_factory = get_community_runtime(request).database.session_factory
    async with session_factory() as session:
        async with session.begin():
            await notifications_repo.mark_all_read(session, user_id=auth.user_id)
            unread = await notifications_repo.unread_count(session, user_id=auth.user_id)
    return ReadAllResponse(unread_count=unread)
