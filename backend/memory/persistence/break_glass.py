"""Break-glass 授权仓储（规格 §13.15）。

表结构见 alembic/versions/0001_memory_core.py：
memory_break_glass_grants / memory_break_glass_audit。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount

_GRANT_COLUMNS = (
    "grant_id, admin_user_id, target_user_id, reason, scopes, "
    "approved_by, expires_at, revoked_at, created_at"
)


async def create_grant(
    session: AsyncSession,
    *,
    grant_id: UUID,
    admin_user_id: UUID,
    target_user_id: UUID,
    reason: str,
    scopes: list[str],
    approved_by: UUID | None,
    expires_at: datetime,
) -> dict[str, Any]:
    await session.execute(
        text(
            """
            INSERT INTO memory_break_glass_grants (
                grant_id, admin_user_id, target_user_id, reason, scopes,
                approved_by, expires_at
            ) VALUES (
                :grant_id, :admin_user_id, :target_user_id, :reason, :scopes,
                :approved_by, :expires_at
            )
            """
        ),
        {
            "grant_id": grant_id,
            "admin_user_id": admin_user_id,
            "target_user_id": target_user_id,
            "reason": reason,
            "scopes": scopes,
            "approved_by": approved_by,
            "expires_at": expires_at,
        },
    )
    grant = await get_grant(session, grant_id)
    assert grant is not None  # 刚插入的行必然存在
    return grant


async def get_grant(session: AsyncSession, grant_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text(f"SELECT {_GRANT_COLUMNS} FROM memory_break_glass_grants WHERE grant_id = :grant_id"),
        {"grant_id": grant_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def revoke_grant(session: AsyncSession, *, grant_id: UUID, revoked_at: datetime) -> bool:
    """返回是否真正撤销（已撤销/不存在返回 False）。"""
    rowcount = await exec_rowcount(
        session,
        text(
            "UPDATE memory_break_glass_grants SET revoked_at = :revoked_at "
            "WHERE grant_id = :grant_id AND revoked_at IS NULL"
        ),
        {"grant_id": grant_id, "revoked_at": revoked_at},
    )
    return rowcount > 0


async def insert_audit(
    session: AsyncSession,
    *,
    audit_id: UUID,
    grant_id: UUID,
    admin_user_id: UUID,
    target_user_id: UUID,
    action: str,
    resource_type: str,
    resource_id: str | None,
    trace_id: str,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO memory_break_glass_audit (
                audit_id, grant_id, admin_user_id, target_user_id,
                action, resource_type, resource_id, trace_id
            ) VALUES (
                :audit_id, :grant_id, :admin_user_id, :target_user_id,
                :action, :resource_type, :resource_id, :trace_id
            )
            """
        ),
        {
            "audit_id": audit_id,
            "grant_id": grant_id,
            "admin_user_id": admin_user_id,
            "target_user_id": target_user_id,
            "action": action,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "trace_id": trace_id,
        },
    )
