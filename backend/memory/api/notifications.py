"""用户通知接口（规格 §19.6 / §13.13）。

GET 默认 limit=20、最大 100，支持 unread_only，返回当前未读总数；
POST /read 幂等：已读通知再次调用仍返回 200 和同一 read_at。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from backend.auth.context import SCOPE_MEMORY_READ, AuthContext
from backend.memory.api.dependencies import (
    ApiRuntime,
    get_runtime,
    get_settings,
    issue_cursor,
    require,
    require_idempotency_key,
    resolve_cursor,
)
from backend.memory.contracts.errors import InvalidPayloadError, NotificationNotFoundError
from backend.memory.contracts.results import (
    MemoryNotification,
    MemoryNotificationPage,
)
from backend.memory.persistence import notifications as notifications_repo
from backend.settings import Settings

router = APIRouter(prefix="/api/v1/memory/notifications", tags=["memory-notifications"])

_USER_ONLY = frozenset({"user"})


def _notification_view(row: dict[str, Any]) -> MemoryNotification:
    return MemoryNotification(
        notification_id=UUID(str(row["notification_id"])),
        event_type=str(row["event_type"]),
        title=str(row["title"]),
        body=str(row["body"]),
        aggregate_type=str(row["aggregate_type"]),
        aggregate_id=str(row["aggregate_id"]),
        read_at=row.get("read_at"),
        created_at=row["created_at"],
    )


@router.get("", response_model=MemoryNotificationPage)
async def list_notifications(
    unread_only: bool = Query(default=False),
    cursor: str | None = Query(default=None, max_length=1000),
    limit: int = Query(default=20, ge=1, le=100),
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
    settings: Settings = Depends(get_settings),
) -> MemoryNotificationPage:
    route = "memory.notifications"
    filters: dict[str, Any] = {"unread_only": unread_only, "limit": limit}
    cursor_created_at: datetime | None = None
    cursor_id: UUID | None = None
    if cursor is not None:
        payload = resolve_cursor(
            settings, cursor, route=route, user_id=auth.user_id, filters=filters
        )
        sort_key = payload.get("sort_key")
        if not isinstance(sort_key, list) or len(sort_key) != 2 or not isinstance(sort_key[0], str):
            raise InvalidPayloadError("cursor sort_key 非法", field="cursor")
        cursor_created_at = datetime.fromisoformat(sort_key[0])
        cursor_id = UUID(str(sort_key[1]))
    async with runtime.session_factory() as session:
        rows = await notifications_repo.list_notifications(
            session,
            user_id=auth.user_id,
            limit=limit + 1,
            cursor_created_at=cursor_created_at,
            cursor_id=cursor_id,
            unread_only=unread_only,
        )
        unread = await notifications_repo.unread_count(session, user_id=auth.user_id)
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = issue_cursor(
            settings,
            route=route,
            user_id=auth.user_id,
            filters=filters,
            sort_key=[last["created_at"].isoformat(), str(last["notification_id"])],
        )
    return MemoryNotificationPage(
        items=[_notification_view(row) for row in rows],
        next_cursor=next_cursor,
        has_more=has_more,
        unread_count=unread,
    )


@router.post("/{notification_id}/read", response_model=MemoryNotification)
async def mark_notification_read(
    notification_id: UUID,
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
    _idempotency_key: str = Depends(require_idempotency_key),
) -> MemoryNotification:
    """幂等已读（§19.6）；他人通知一律 404（§18.4）。"""
    async with runtime.session_factory() as session:
        async with session.begin():
            row = await notifications_repo.mark_read(
                session, user_id=auth.user_id, notification_id=notification_id
            )
    if row is None:
        raise NotificationNotFoundError("通知不存在")
    return _notification_view(row)


class MemoryNotificationReadAllResponse(BaseModel):
    """read-all 统一响应（方案 community §8.6 冻结：{"unread_count": 0}）。"""

    model_config = ConfigDict(extra="forbid")

    unread_count: int


@router.post("/read-all", response_model=MemoryNotificationReadAllResponse)
async def mark_all_notifications_read(
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
) -> MemoryNotificationReadAllResponse:
    """全部已读（D14：只更新当前认证用户的未读记录；重复调用返回 200 与当前计数）。"""
    async with runtime.session_factory() as session:
        async with session.begin():
            await notifications_repo.mark_all_read(session, user_id=auth.user_id)
            unread = await notifications_repo.unread_count(session, user_id=auth.user_id)
    return MemoryNotificationReadAllResponse(unread_count=unread)
