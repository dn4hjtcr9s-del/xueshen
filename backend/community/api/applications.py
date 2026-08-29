"""Community 建吧申请接口（community-rebuild-plan.md §八 #13-#15）。"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from backend.auth.context import AuthContext
from backend.community.contracts.api import (
    BoardApplicationRequest,
    BoardApplicationView,
    Page,
)
from backend.community.services.board_application_service import BoardApplicationService
from backend.shared.auth_context import get_auth_context

from .cursor import issue_application_cursor, resolve_application_cursor
from .dependencies import get_application_service, rate_limit

router = APIRouter(prefix="/api/v1/community", tags=["community"])


@router.post(
    "/applications",
    response_model=BoardApplicationView,
    status_code=201,
    dependencies=[Depends(rate_limit("community.application"))],
)
async def create_application(
    payload: BoardApplicationRequest,
    auth: AuthContext = Depends(get_auth_context),
    service: BoardApplicationService = Depends(get_application_service),
) -> BoardApplicationView:
    return await service.create_application(
        applicant_id=auth.user_id,
        name=payload.name,
        slug=payload.slug,
        description=payload.description,
        reason=payload.reason,
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
    filters = {"kind": "mine"}
    resolved = resolve_application_cursor(
        request, "community.applications.mine", cursor, filters=filters
    )
    after = None
    if resolved is not None:
        after = (resolved["created_at"], UUID(resolved["application_id"]))
    rows, next_after, has_more = await service.list_mine(
        auth.user_id,
        limit=limit,
        after=after,
    )
    next_cursor = issue_application_cursor(
        request,
        route="community.applications.mine",
        filters=filters,
        next_after=next_after,
    )
    return Page[BoardApplicationView](items=rows, next_cursor=next_cursor, has_more=has_more)
