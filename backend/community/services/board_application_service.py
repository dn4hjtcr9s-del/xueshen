"""建吧申请与审核服务（community-rebuild-plan.md §7.4）。"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.community.contracts.api import BoardApplicationView
from backend.community.contracts.errors import (
    ApplicationAlreadyReviewedError,
    ApplicationDuplicatePendingError,
    BoardNameConflictError,
    BoardSlugReservedError,
    CommunityContentInvalidError,
    CommunityIdempotencyConflictError,
    CommunityNotFoundError,
    RejectReasonInvalidError,
)
from backend.community.persistence import board_applications as applications_repo
from backend.community.persistence import boards as boards_repo
from backend.community.persistence import idempotency as idem_repo
from backend.community.persistence import notifications as notifications_repo
from backend.community.services.content_safety import (
    validate_application_reason,
    validate_board_description,
    validate_board_name,
)
from backend.community.services.notification_templates import (
    application_approved_body,
    application_rejected_body,
)
from backend.settings import Settings
from backend.shared.cursor import canonical_json

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


def _idempotency_payload_hash(values: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()


_IDEM_CONFLICT_MSG = "同一 Idempotency-Key 已用于不同请求"


def _normalize_slug(raw: str) -> str:
    return raw.strip().lower()


def validate_application_input(
    settings: Settings,
    *,
    name: str,
    slug: str,
    description: str,
    reason: str,
) -> tuple[str, str, str, str]:
    """应用层校验；返回规范化后的值。

    §7.5：吧名/简介/申请理由/slug 的内容校验（空/长度/正则）→ 422
    `COMMUNITY_CONTENT_INVALID`（field 为对应字段）；仅保留字 → 422
    `BOARD_SLUG_RESERVED`。名称被占等业务冲突由 service 另行映射 409。
    """
    name = validate_board_name(name, max_chars=settings.community_board_name_max_chars)
    slug = _normalize_slug(slug)
    description = validate_board_description(
        description, max_chars=settings.community_board_description_max_chars
    )
    reason = validate_application_reason(
        reason, max_chars=settings.community_application_reason_max_chars
    )
    if len(slug) < 2 or len(slug) > settings.community_board_slug_max_chars:
        raise CommunityContentInvalidError(
            f"slug 长度须在 2–{settings.community_board_slug_max_chars} 字符之间", field="slug"
        )
    if not _SLUG_RE.match(slug):
        raise CommunityContentInvalidError("slug 只能包含小写字母、数字和单个连字符", field="slug")
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
    ) -> tuple[list[BoardApplicationView], tuple[str, str] | None, bool]:
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
            next_after: tuple[str, str] | None = None
            if page_rows:
                last = page_rows[-1]
                # cursor sort_key 须 JSON 可序列化：application_id 统一为 str
                next_after = (last["created_at"].isoformat(), str(last["application_id"]))
            return [_to_view(row) for row in page_rows], next_after, has_more

    async def list_admin(
        self,
        *,
        status: str | None,
        limit: int,
        after: tuple[str, UUID] | None,
    ) -> tuple[list[BoardApplicationView], tuple[str, str] | None, bool]:
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
            next_after: tuple[str, str] | None = None
            if page_rows:
                last = page_rows[-1]
                # cursor sort_key 须 JSON 可序列化：application_id 统一为 str
                next_after = (last["created_at"].isoformat(), str(last["application_id"]))
            return [_to_view(row) for row in page_rows], next_after, has_more

    async def create_application(
        self,
        *,
        applicant_id: UUID,
        name: str,
        slug: str,
        description: str,
        reason: str,
        idempotency_key: str,
    ) -> BoardApplicationView:
        name, slug, description, reason = validate_application_input(
            self.settings, name=name, slug=slug, description=description, reason=reason
        )
        # §7.11：create_application 幂等 hash 输入 = 规范化后值
        payload_hash = _idempotency_payload_hash(
            {
                "description": description,
                "name": name,
                "reason": reason,
                "slug": slug,
            }
        )
        application_id = uuid4()
        async with self._session_factory() as session:
            async with session.begin():
                existing = await idem_repo.get_request(
                    session,
                    user_id=applicant_id,
                    operation="create_application",
                    idempotency_key=idempotency_key,
                )
                if existing is not None:
                    if existing["payload_hash"] != payload_hash:
                        raise CommunityIdempotencyConflictError(_IDEM_CONFLICT_MSG)
                    row = await applications_repo.get_application_by_id(
                        session, existing["resource_id"]
                    )
                    return _to_view(row)

                won = await idem_repo.insert_request(
                    session,
                    user_id=applicant_id,
                    operation="create_application",
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    resource_type="application",
                    resource_id=application_id,
                    retention_days=self.settings.community_idempotency_retention_days,
                )
                if not won:
                    existing = await idem_repo.get_request(
                        session,
                        user_id=applicant_id,
                        operation="create_application",
                        idempotency_key=idempotency_key,
                    )
                    if existing is None or existing["payload_hash"] != payload_hash:
                        raise CommunityIdempotencyConflictError("幂等键并发冲突且无法读取原资源")
                    row = await applications_repo.get_application_by_id(
                        session, existing["resource_id"]
                    )
                    return _to_view(row)

                # 申请提交时即查 boards 冲突（§7.4 D47）
                if await boards_repo.check_board_name_conflict(session, name, slug):
                    raise BoardNameConflictError("吧名或标识已被现有板块占用")

                existing = await applications_repo.get_pending_application_by_applicant(
                    session, applicant_id
                )
                if existing is not None:
                    raise ApplicationDuplicatePendingError("你已有一个待审核的建吧申请")

                try:
                    await applications_repo.insert_application(
                        session,
                        application_id=application_id,
                        applicant_id=applicant_id,
                        name=name,
                        slug=slug,
                        description=description,
                        reason=reason,
                    )
                except IntegrityError as exc:
                    # 并发/重试触发 pending 唯一索引或 boards 冲突
                    msg = str(exc).lower()
                    if "uq_community_board_applications_pending" in msg:
                        raise ApplicationDuplicatePendingError(
                            "你已有一个待审核的建吧申请"
                        ) from exc
                    if "uq_community_boards_slug" in msg or "uq_community_boards_name" in msg:
                        raise BoardNameConflictError("吧名或标识已被现有板块占用") from exc
                    raise
                row = await applications_repo.get_application_by_id(session, application_id)
                return _to_view(row)

    async def approve(
        self,
        *,
        application_id: UUID,
        reviewer_id: UUID,
    ) -> BoardApplicationView:
        """审核通过（D38）：锁行 → INSERT boards → 单语句 UPDATE → 通知。"""
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
                try:
                    await boards_repo.insert_board(
                        session,
                        board_id=board_id,
                        slug=application["slug"],
                        name=application["name"],
                        description=application["description"],
                        created_by=application["applicant_id"],
                    )
                except IntegrityError as exc:
                    if "uq_community_boards_slug" in str(exc).lower():
                        raise BoardNameConflictError("该 slug 已被现有板块占用") from exc
                    if "uq_community_boards_name" in str(exc).lower():
                        raise BoardNameConflictError("该吧名已被现有板块占用") from exc
                    raise

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
        """审核拒绝（D38）：锁行 → 单语句 UPDATE → 通知；0 行 → 409。"""
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

                updated = await applications_repo.reject_application(
                    session,
                    application_id=application_id,
                    reviewer_id=reviewer_id,
                    reason=reason,
                )
                if updated == 0:
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
