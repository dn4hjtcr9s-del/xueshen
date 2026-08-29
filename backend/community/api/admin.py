"""Community 管理员审核接口（community-rebuild-plan.md §八 #18-#20）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request

from backend.auth.context import AuthContext
from backend.community.contracts.api import (
    BoardApplicationView,
    Page,
    RejectApplicationRequest,
)
from backend.community.services.board_application_service import BoardApplicationService
from backend.shared.auth_context import get_auth_context

from .cursor import issue_application_cursor, resolve_application_cursor
from .dependencies import get_application_service, rate_limit, require_community_admin

router = APIRouter(prefix="/api/v1/community/admin", tags=["community-admin"])


@router.get(
    "/applications",
    response_model=Page[BoardApplicationView],
    dependencies=[Depends(rate_limit("community.admin.review"))],
)
async def list_applications_for_review(
    request: Request,
    status: str | None = Query(None, pattern=r"^(pending|approved|rejected|all)$"),
    cursor: str | None = None,
    limit: int = Query(20, ge=1, le=50),
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_community_admin),
    service: BoardApplicationService = Depends(get_application_service),
) -> Page[BoardApplicationView]:
    filters: dict[str, Any] = {"status": status or "all"}
    route = "community.admin.applications"
    resolved = resolve_application_cursor(request, route, cursor, filters=filters)
    after = None
    if resolved is not None:
        after = (resolved["created_at"], UUID(resolved["application_id"]))
    rows, next_after, has_more = await service.list_admin(
        status=status,
        limit=limit,
        after=after,
    )
    next_cursor = issue_application_cursor(
        request,
        route=route,
        filters=filters,
        next_after=next_after,
    )
    return Page[BoardApplicationView](items=rows, next_cursor=next_cursor, has_more=has_more)


@router.post(
    "/applications/{application_id}/approve",
    response_model=BoardApplicationView,
    dependencies=[Depends(rate_limit("community.admin.review"))],
)
async def approve_application(
    application_id: UUID,
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_community_admin),
    service: BoardApplicationService = Depends(get_application_service),
) -> BoardApplicationView:
    return await service.approve(
        application_id=application_id,
        reviewer_id=auth.user_id,
    )


@router.post(
    "/applications/{application_id}/reject",
    response_model=BoardApplicationView,
    dependencies=[Depends(rate_limit("community.admin.review"))],
)
async def reject_application(
    application_id: UUID,
    payload: RejectApplicationRequest,
    auth: AuthContext = Depends(get_auth_context),
    _: AuthContext = Depends(require_community_admin),
    service: BoardApplicationService = Depends(get_application_service),
) -> BoardApplicationView:
    return await service.reject(
        application_id=application_id,
        reviewer_id=auth.user_id,
        reason=payload.reason,
    )
