"""Community 公共游标签发/解析工具。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from fastapi import Request

from backend.community.contracts.errors import CommunityCursorInvalidError
from backend.shared.cursor import CursorError
from backend.shared.cursor import issue_cursor as shared_issue_cursor
from backend.shared.cursor import resolve_cursor as shared_resolve_cursor

_UUID_ZERO = UUID(int=0)


def resolve_public_cursor(
    request: Request,
    route: str,
    token: str | None,
    *,
    filters: dict[str, Any],
) -> dict[str, Any] | None:
    if token is None:
        return None
    settings = request.app.state.settings
    try:
        return shared_resolve_cursor(
            settings,
            token,
            route=route,
            user_id=_UUID_ZERO,
            filters=filters,
            bind_principal=False,
        )
    except CursorError as exc:
        raise CommunityCursorInvalidError(str(exc)) from exc


def issue_public_cursor(
    request: Request,
    *,
    route: str,
    filters: dict[str, Any],
    next_after: Sequence[Any] | None,
) -> str | None:
    if next_after is None:
        return None
    settings = request.app.state.settings
    return shared_issue_cursor(
        settings,
        route=route,
        user_id=_UUID_ZERO,
        filters=filters,
        sort_key=list(next_after),
        bind_principal=False,
    )


def resolve_private_cursor(
    request: Request,
    route: str,
    token: str | None,
    *,
    user_id: UUID,
    filters: dict[str, Any],
) -> dict[str, Any] | None:
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
        raise CommunityCursorInvalidError(str(exc)) from exc


def issue_private_cursor(
    request: Request,
    *,
    route: str,
    user_id: UUID,
    filters: dict[str, Any],
    next_after: Sequence[Any] | None,
) -> str | None:
    if next_after is None:
        return None
    settings = request.app.state.settings
    return shared_issue_cursor(
        settings,
        route=route,
        user_id=user_id,
        filters=filters,
        sort_key=list(next_after),
        bind_principal=True,
    )


def resolve_application_cursor(
    request: Request,
    route: str,
    token: str | None,
    *,
    user_id: UUID,
    filters: dict[str, Any],
) -> dict[str, Any] | None:
    # §八：mine/审核列表与用户身份相关，使用私有游标（bind_principal=True），
    # 防止游标跨用户复用（#14/#15）。
    return resolve_private_cursor(request, route, token, user_id=user_id, filters=filters)


def issue_application_cursor(
    request: Request,
    *,
    route: str,
    user_id: UUID,
    filters: dict[str, Any],
    next_after: Sequence[Any] | None,
) -> str | None:
    return issue_private_cursor(
        request, route=route, user_id=user_id, filters=filters, next_after=next_after
    )
