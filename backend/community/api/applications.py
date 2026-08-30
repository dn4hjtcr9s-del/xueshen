"""Community 建吧申请接口（community-rebuild-plan.md §八 #13-#15）。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request

from backend.auth.context import AuthContext
from backend.community.contracts.api import (
    BoardApplicationRequest,
    BoardApplicationView,
    Page,
)
from backend.community.contracts.errors import CommunityCursorInvalidError
from backend.community.services.board_application_service import BoardApplicationService
from backend.shared.auth_context import get_auth_context

from .cursor import issue_application_cursor, resolve_application_cursor
from .dependencies import (
    IDEMPOTENCY_KEY_RE,
    get_application_service,
    rate_limit,
    require_idempotency_key,
)

router = APIRouter(prefix="/api/v1/community", tags=["community"])


@router.post(
    "/applications",
    response_model=BoardApplicationView,
    status_code=201,
    dependencies=[Depends(rate_limit("community.application"))],
)
async def create_application(
    payload: BoardApplicationRequest,
    idempotency_key: str | None = Header(
        default=None, alias="Idempotency-Key", pattern=IDEMPOTENCY_KEY_RE
    ),
    auth: AuthContext = Depends(get_auth_context),
    service: BoardApplicationService = Depends(get_application_service),
) -> BoardApplicationView:
    """申请建吧（§八 #13）：创建类接口必须 Idempotency-Key（§7.11）。"""
    key = require_idempotency_key(idempotency_key)
    return await service.create_application(
        applicant_id=auth.user_id,
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
        reason=payload.reason,
        idempotency_key=key,
    )


@router.get(
    "/applications/mine",
    response_model=Page[BoardApplicationView],
    dependencies=[Depends(rate_limit("community.read"))],
)
async def list_my_applications(
    request: Request,
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    auth: AuthContext = Depends(get_auth_context),
    service: BoardApplicationService = Depends(get_application_service),
) -> Page[BoardApplicationView]:
    # §八 #14：私有游标绑定当前用户；normalized_filters 固定 {"status": "mine"}
    filters = {"status": "mine"}
    resolved = resolve_application_cursor(
        request,
        "community.applications.mine",
        cursor,
        user_id=auth.user_id,
        filters=filters,
    )
    after = None
    if resolved is not None:
        sort_key = resolved["sort_key"]
        if not isinstance(sort_key, list) or len(sort_key) != 2:
            raise CommunityCursorInvalidError("游标缺少完整排序键")
        after = (sort_key[0], UUID(str(sort_key[1])))
    rows, next_after, has_more = await service.list_mine(
        auth.user_id,
        limit=limit,
        after=after,
    )
    next_cursor = issue_application_cursor(
        request,
        route="community.applications.mine",
        user_id=auth.user_id,
        filters=filters,
        next_after=next_after,
    )
    return Page[BoardApplicationView](items=rows, next_cursor=next_cursor, has_more=has_more)
