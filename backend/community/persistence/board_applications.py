"""建吧申请持久化（community-rebuild-plan.md §7.3/§7.4）。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def insert_application(
    session: AsyncSession,
    *,
    application_id: UUID,
    applicant_id: UUID,
    name: str,
    slug: str,
    description: str,
    reason: str,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO community_board_applications (
                application_id, applicant_id, name, slug, description, reason,
                status, created_at, updated_at
            ) VALUES (
                :application_id, :applicant_id, :name, :slug, :description, :reason,
                'pending', now(), now()
            )
            """
        ),
        {
            "application_id": application_id,
            "applicant_id": applicant_id,
            "name": name,
            "slug": slug,
            "description": description,
            "reason": reason,
        },
    )


async def get_application_by_id(
    session: AsyncSession,
    application_id: UUID,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    query = "SELECT * FROM community_board_applications WHERE application_id = :id"
    if for_update:
        query += " FOR UPDATE"
    row = await session.execute(text(query), {"id": application_id})
    result = row.mappings().fetchone()
    return dict(result) if result is not None else None


async def get_pending_application_by_applicant(
    session: AsyncSession,
    applicant_id: UUID,
) -> dict[str, Any] | None:
    row = await session.execute(
        text(
            """
            SELECT * FROM community_board_applications
            WHERE applicant_id = :applicant_id AND status = 'pending'
            """
        ),
        {"applicant_id": applicant_id},
    )
    result = row.mappings().fetchone()
    return dict(result) if result is not None else None


async def list_applications_by_applicant(
    session: AsyncSession,
    applicant_id: UUID,
    *,
    limit: int,
    last_created_at: str | None = None,
    last_application_id: str | None = None,
) -> list[dict[str, Any]]:
    """mine 列表：created_at DESC, application_id DESC。"""
    sql = "SELECT * FROM community_board_applications WHERE applicant_id = :applicant_id"
    params: dict[str, Any] = {"applicant_id": applicant_id, "limit": limit + 1}
    if last_created_at is not None and last_application_id is not None:
        sql += " AND (created_at, application_id) < (:last_created_at, :last_application_id)"
        params["last_created_at"] = last_created_at
        params["last_application_id"] = last_application_id
    sql += " ORDER BY created_at DESC, application_id DESC LIMIT :limit"
    rows = await session.execute(text(sql), params)
    return [dict(r) for r in rows.mappings().fetchall()]


async def list_applications_for_admin(
    session: AsyncSession,
    *,
    status: str | None,
    limit: int,
    last_created_at: str | None = None,
    last_application_id: str | None = None,
) -> list[dict[str, Any]]:
    """admin 列表：created_at ASC, application_id ASC。"""
    conditions = []
    params: dict[str, Any] = {"limit": limit + 1}
    if status and status != "all":
        conditions.append("status = :status")
        params["status"] = status
    if last_created_at is not None and last_application_id is not None:
        conditions.append("(created_at, application_id) > (:last_created_at, :last_application_id)")
        params["last_created_at"] = last_created_at
        params["last_application_id"] = last_application_id

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    sql = (
        f"SELECT * FROM community_board_applications {where} "
        "ORDER BY created_at ASC, application_id ASC LIMIT :limit"
    )
    rows = await session.execute(text(sql), params)
    return [dict(r) for r in rows.mappings().fetchall()]


async def approve_application(
    session: AsyncSession,
    *,
    application_id: UUID,
    reviewer_id: UUID,
    board_id: UUID,
) -> None:
    await session.execute(
        text(
            """
            UPDATE community_board_applications
            SET status = 'approved', reviewer_id = :reviewer_id,
                reviewed_at = now(), board_id = :board_id, updated_at = now()
            WHERE application_id = :application_id AND status = 'pending'
            """
        ),
        {
            "application_id": application_id,
            "reviewer_id": reviewer_id,
            "board_id": board_id,
        },
    )


async def reject_application(
    session: AsyncSession,
    *,
    application_id: UUID,
    reviewer_id: UUID,
    reason: str,
) -> None:
    await session.execute(
        text(
            """
            UPDATE community_board_applications
            SET status = 'rejected', reviewer_id = :reviewer_id,
                reviewed_at = now(), reject_reason = :reason, updated_at = now()
            WHERE application_id = :application_id AND status = 'pending'
            """
        ),
        {
            "application_id": application_id,
            "reviewer_id": reviewer_id,
            "reason": reason,
        },
    )


async def count_applications_for_admin(
    session: AsyncSession,
    *,
    status: str | None,
) -> int:
    conditions = []
    params: dict[str, Any] = {}
    if status and status != "all":
        conditions.append("status = :status")
        params["status"] = status
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    row = await session.execute(
        text(f"SELECT count(*) FROM community_board_applications {where}"),
        params,
    )
    return int(row.scalar() or 0)
