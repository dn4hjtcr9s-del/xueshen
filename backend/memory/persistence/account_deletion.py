"""account_deletion_manifest 仓储（规格 §13.16 / §19.7）。

manifest 只保存 user_hash（privacy-audit 域摘要），不包含可还原的用户正文；
user_hash UNIQUE 保证同一用户最多一条 manifest。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount


async def insert_manifest(
    session: AsyncSession,
    *,
    account_deletion_id: UUID,
    user_hash: str,
    user_hash_key_version: str,
    requested_at: datetime,
    backup_retention_until: datetime,
) -> bool:
    """创建 manifest；user_hash 冲突（同一用户已有 manifest）返回 False。"""
    rowcount = await exec_rowcount(
        session,
        text(
            """
            INSERT INTO account_deletion_manifest (
                account_deletion_id, user_hash, user_hash_key_version,
                status, requested_at, backup_retention_until
            ) VALUES (
                :account_deletion_id, :user_hash, :user_hash_key_version,
                'requested', :requested_at, :backup_retention_until
            )
            ON CONFLICT (user_hash) DO NOTHING
            """
        ),
        {
            "account_deletion_id": account_deletion_id,
            "user_hash": user_hash,
            "user_hash_key_version": user_hash_key_version,
            "requested_at": requested_at,
            "backup_retention_until": backup_retention_until,
        },
    )
    return rowcount == 1


async def get_manifest_by_user_hash(
    session: AsyncSession, *, user_hash: str
) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM account_deletion_manifest WHERE user_hash = :user_hash"),
        {"user_hash": user_hash},
    )
    row = result.mappings().first()
    return dict(row) if row else None
