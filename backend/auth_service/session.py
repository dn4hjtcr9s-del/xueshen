"""Refresh 会话仓储（方案 §4.4）：轮换、重放检测、family 撤销与过期清理。

- 每次 refresh 轮换（旧 token 标记作废），数据库操作使用 SELECT ... FOR UPDATE。
- 已作废 token 再次提交 = 重放迹象 → 撤销整族（不设宽限期）。
- logout 撤销当前 family 全部 token；用户 disabled 时撤销其全部 family。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth_service.database import REFRESH_TOKEN_TTL_DAYS, refresh_expiry


async def _rowcount(result: Any) -> int:
    """text() 的 Result 不暴露 rowcount 类型（与 memory 侧 exec_rowcount 同法）。"""
    from sqlalchemy.engine import CursorResult

    if isinstance(result, CursorResult):
        return result.rowcount
    return 0


async def get_token_row_for_update(
    session: AsyncSession, token_hash: bytes
) -> dict[str, Any] | None:
    """行锁读取 refresh token 行（轮换/重放判定前必须持有）。"""
    result = await session.execute(
        text(
            """
            SELECT token_hash, user_id, family_id, expires_at, revoked_at
            FROM refresh_tokens
            WHERE token_hash = :token_hash
            FOR UPDATE
            """
        ),
        {"token_hash": token_hash},
    )
    row = result.first()
    return dict(row._mapping) if row is not None else None


async def insert_refresh_token(
    session: AsyncSession,
    *,
    token_hash: bytes,
    user_id: UUID,
    family_id: UUID,
    expires_at: datetime,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO refresh_tokens (token_hash, user_id, family_id, expires_at)
            VALUES (:token_hash, :user_id, :family_id, :expires_at)
            """
        ),
        {
            "token_hash": token_hash,
            "user_id": user_id,
            "family_id": family_id,
            "expires_at": expires_at,
        },
    )


async def revoke_token(session: AsyncSession, token_hash: bytes) -> None:
    """作废单个 token（轮换时把旧 token 标记为已撤销）。"""
    await session.execute(
        text(
            "UPDATE refresh_tokens SET revoked_at = now() "
            "WHERE token_hash = :token_hash AND revoked_at IS NULL"
        ),
        {"token_hash": token_hash},
    )


async def revoke_family(session: AsyncSession, family_id: UUID) -> int:
    """撤销整族全部未撤销 token；返回受影响行数。"""
    result = await session.execute(
        text(
            "UPDATE refresh_tokens SET revoked_at = now() "
            "WHERE family_id = :family_id AND revoked_at IS NULL"
        ),
        {"family_id": family_id},
    )
    return (await _rowcount(result)) or 0


async def revoke_all_families(session: AsyncSession, user_id: UUID) -> int:
    """撤销用户全部 family（禁用账号时调用，方案 §4.4）。"""
    result = await session.execute(
        text(
            "UPDATE refresh_tokens SET revoked_at = now() "
            "WHERE user_id = :user_id AND revoked_at IS NULL"
        ),
        {"user_id": user_id},
    )
    return (await _rowcount(result)) or 0


async def delete_expired_families(session: AsyncSession, *, older_than_days: int = 30) -> int:
    """删除过期超过 30 天的 family 全部行（方案 §4.4，scheduler 每日执行）。"""
    cutoff = datetime.now(UTC) - timedelta(days=REFRESH_TOKEN_TTL_DAYS + older_than_days)
    result = await session.execute(
        text(
            """
            DELETE FROM refresh_tokens
            WHERE family_id IN (
                SELECT family_id FROM refresh_tokens
                GROUP BY family_id
                HAVING max(expires_at) < :cutoff
            )
            """
        ),
        {"cutoff": cutoff},
    )
    return (await _rowcount(result)) or 0


async def rotate(
    session: AsyncSession,
    *,
    old_hash: bytes,
    new_hash: bytes,
    user_id: UUID,
    family_id: UUID,
    now: datetime,
) -> None:
    """同事务内完成轮换：旧 token 作废 + 新 token 落库。"""
    await revoke_token(session, old_hash)
    await insert_refresh_token(
        session,
        token_hash=new_hash,
        user_id=user_id,
        family_id=family_id,
        expires_at=refresh_expiry(now),
    )
