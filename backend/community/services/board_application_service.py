"""建吧申请与审核服务（community-rebuild-plan.md §7.4）。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.community.contracts.api import BoardApplicationView
from backend.community.contracts.errors import (
    ApplicationAlreadyReviewedError,
    ApplicationDuplicatePendingError,
    BoardNameConflictError,
    BoardSlugReservedError,
    CommunityNotFoundError,
    RejectReasonInvalidError,
)
from backend.community.persistence import board_applications as applications_repo
from backend.community.persistence import boards as boards_repo
from backend.community.persistence import notifications as notifications_repo
from backend.community.services.notification_templates import (
    application_approved_body,
    application_rejected_body,
)
from backend.settings import Settings

_RESERVED_SLUGS: frozenset[str] = frozenset(
    {
        "applications",
        "admin",
        "posts",
        "uploads",
        "new",
        "mine",
        "replies",
        "notifications",
        "local-uploads",
    }
)

_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,28}[a-z0-9]$")


def _normalize_slug(raw: str) -> str:
    return raw.strip().lower()


def _normalize_name(raw: str) -> str:
    name = raw.strip()
    return unicodedata.normalize("NFC", name)


def validate_application_input(
    settings: Settings,
    *,
    name: str,
    slug: str,
    description: str,
    reason: str,
) -> tuple[str, str, str, str]:
    """应用层校验；返回规范化后的值。"""
    name = _normalize_name(name)
    slug = _normalize_slug(slug)
    description = description.strip()
    reason = reason.strip()

    if not name or len(name) > settings.community_board_name_max_chars:
        raise BoardNameConflictError(
            f"吧名长度须在 1–{settings.community_board_name_max_chars} 字符之间"
        )
    if len(description) > settings.community_board_description_max_chars:
        raise BoardNameConflictError(
            f"简介长度不得超过 {settings.community_board_description_max_chars} 字符"
        )
    if not reason or len(reason) > settings.community_application_reason_max_chars:
        raise RejectReasonInvalidError(
            f"申请理由长度须在 1–{settings.community_application_reason_max_chars} 字符之间"
        )
    if len(slug) < 2 or len(slug) > settings.community_board_slug_max_chars:
        raise BoardSlugReservedError("slug 长度须在 2–30 字符之间")
    if not _SLUG_RE.match(slug):
        raise BoardSlugReservedError("slug 只能包含小写字母、数字和单个连字符")
    if slug in _RESERVED_SLUGS:
        raise BoardSlugReservedError("slug 为系统保留字")
    return name, slug, description, reason


class BoardApplicationService:
    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory

    async def list_mine(
        self,
        applicant_id: UUID,
        *,
        limit: int,
        after: tuple[str, UUID] | None,
    ) -> tuple[list[BoardApplicationView], tuple[str, UUID] | None, bool]:
        async with self._session_factory() as session:
            last_created_at = after[0] if after else None
            last_application_id = str(after[1]) if after else None
            rows = await applications_repo.list_applications_by_applicant(
                session,
                applicant_id,
                limit=limit,
                last_created_at=last_created_at,
                last_application_id=last_application_id,
            )
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            next_after: tuple[str, UUID] | None = None
            if page_rows:
                last = page_rows[-1]
                next_after = (last["created_at"].isoformat(), last["application_id"])
            return [_to_view(row) for row in page_rows], next_after, has_more

    async def list_admin(
        self,
        *,
        status: str | None,
        limit: int,
        after: tuple[str, UUID] | None,
    ) -> tuple[list[BoardApplicationView], tuple[str, UUID] | None, bool]:
        async with self._session_factory() as session:
            last_created_at = after[0] if after else None
            last_application_id = str(after[1]) if after else None
            rows = await applications_repo.list_applications_for_admin(
                session,
                status=status,
                limit=limit,
                last_created_at=last_created_at,
                last_application_id=last_application_id,
            )
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            next_after: tuple[str, UUID] | None = None
            if page_rows:
                last = page_rows[-1]
                next_after = (last["created_at"].isoformat(), last["application_id"])
            return [_to_view(row) for row in page_rows], next_after, has_more

    async def create_application(
        self,
        *,
        applicant_id: UUID,
        name: str,
        slug: str,
        description: str,
        reason: str,
    ) -> BoardApplicationView:
        name, slug, description, reason = validate_application_input(
            self.settings, name=name, slug=slug, description=description, reason=reason
        )

        async with self._session_factory() as session:
            async with session.begin():
                # 申请提交时即查 boards 冲突
                if await boards_repo.check_board_name_conflict(session, name, slug):
                    raise BoardNameConflictError("吧名或标识已被现有板块占用")

                existing = await applications_repo.get_pending_application_by_applicant(
                    session, applicant_id
                )
                if existing is not None:
                    raise ApplicationDuplicatePendingError("你已有一个待审核的建吧申请")

                application_id = uuid4()
                await applications_repo.insert_application(
                    session,
                    application_id=application_id,
                    applicant_id=applicant_id,
                    name=name,
                    slug=slug,
                    description=description,
                    reason=reason,
                )
                row = await applications_repo.get_application_by_id(session, application_id)
                return _to_view(row)

    async def approve(
        self,
        *,
        application_id: UUID,
        reviewer_id: UUID,
    ) -> BoardApplicationView:
        async with self._session_factory() as session:
            async with session.begin():
                application = await applications_repo.get_application_by_id(
                    session, application_id, for_update=True
                )
                if application is None:
                    raise CommunityNotFoundError("申请不存在")
                if application["status"] != "pending":
                    raise ApplicationAlreadyReviewedError("该申请已被审核")

                board_id = uuid4()
                await boards_repo.insert_board(
                    session,
                    board_id=board_id,
                    slug=application["slug"],
                    name=application["name"],
                    description=application["description"],
                    created_by=application["applicant_id"],
                )

                await applications_repo.approve_application(
                    session,
                    application_id=application_id,
                    reviewer_id=reviewer_id,
                    board_id=board_id,
                )

                await notifications_repo.insert_notification(
                    session,
                    notification_id=uuid4(),
                    recipient_user_id=application["applicant_id"],
                    actor_user_id=reviewer_id,
                    event_type="application_approved",
                    post_id=None,
                    reply_id=None,
                    board_slug=application["slug"],
                    title="建吧申请已通过",
                    body=application_approved_body(application["name"]),
                    dedupe=notifications_repo.dedupe_key(
                        "application_approved", application_id=application_id
                    ),
                )

                row = await applications_repo.get_application_by_id(session, application_id)
                return _to_view(row)

    async def reject(
        self,
        *,
        application_id: UUID,
        reviewer_id: UUID,
        reason: str,
    ) -> BoardApplicationView:
        reason = reason.strip()
        if not reason or len(reason) > self.settings.community_reject_reason_max_chars:
            raise RejectReasonInvalidError(
                f"拒绝理由长度须在 1–{self.settings.community_reject_reason_max_chars} 字符之间"
            )

        async with self._session_factory() as session:
            async with session.begin():
                application = await applications_repo.get_application_by_id(
                    session, application_id, for_update=True
                )
                if application is None:
                    raise CommunityNotFoundError("申请不存在")
                if application["status"] != "pending":
                    raise ApplicationAlreadyReviewedError("该申请已被审核")

                result = await session.execute(
                    text(
                        "UPDATE community_board_applications SET status='rejected', "
                        "reviewer_id=:reviewer_id, reviewed_at=now(), reject_reason=:reason, "
                        "updated_at=now() WHERE application_id=:id AND status='pending'"
                    ),
                    {"reviewer_id": reviewer_id, "reason": reason, "id": application_id},
                )
                if isinstance(result, CursorResult) and result.rowcount == 0:
                    raise ApplicationAlreadyReviewedError("该申请已被审核")

                await notifications_repo.insert_notification(
                    session,
                    notification_id=uuid4(),
                    recipient_user_id=application["applicant_id"],
                    actor_user_id=reviewer_id,
                    event_type="application_rejected",
                    post_id=None,
                    reply_id=None,
                    board_slug=application["slug"],
                    title="建吧申请未通过",
                    body=application_rejected_body(application["name"], reason),
                    dedupe=notifications_repo.dedupe_key(
                        "application_rejected", application_id=application_id
                    ),
                )

                row = await applications_repo.get_application_by_id(session, application_id)
                return _to_view(row)


def _to_view(row: dict[str, Any] | None) -> BoardApplicationView:
    if row is None:
        raise CommunityNotFoundError("申请不存在")
    return BoardApplicationView(
        application_id=row["application_id"],
        name=row["name"],
        slug=row["slug"],
        description=row["description"],
        reason=row["reason"],
        status=row["status"],
        board_id=row["board_id"],
        reviewed_at=row["reviewed_at"],
        reject_reason=row["reject_reason"],
        created_at=row["created_at"],
    )
