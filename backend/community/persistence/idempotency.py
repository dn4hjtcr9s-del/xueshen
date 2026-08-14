"""community_idempotency_requests 仓储（方案 §7.6 / D40）。

发帖/回复的创建幂等不能只依赖 Outbox（API 事务在 Outbox 写入前就可能被
客户端重试），因此独立幂等表。唯一键 (user_id, operation, idempotency_key)；
payload_hash 由 canonical_json（D40）对规范化后请求模型计算。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount


async def insert_request(
    session: AsyncSession,
    *,
    user_id: UUID,
    operation: str,
    idempotency_key: str,
    payload_hash: str,
    resource_type: str,
    resource_id: UUID,
    retention_days: int,
) -> bool:
    """抢占幂等键（Critical 1 修复：并发裁决由唯一约束完成）。

    两步原子操作：
    1. 删除过期占位行（唯一约束含过期行，必须先删才能让过期键重新可用，
       否则"7 天保留期后旧行占位不刷新"会破坏 §8.3 同键语义）；
    2. INSERT ... ON CONFLICT DO NOTHING：并发同键时唯一约束会阻塞至对方
       事务提交，随后返回 False（本事务未赢得键），调用方应重读并返回
       原资源（此时幂等行必然可见）。
    """
    now = datetime.now(UTC)
    await session.execute(
        text(
            "DELETE FROM community_idempotency_requests "
            "WHERE user_id = :user_id AND operation = :operation "
            "  AND idempotency_key = :idempotency_key AND expires_at <= :now"
        ),
        {
            "user_id": user_id,
            "operation": operation,
            "idempotency_key": idempotency_key,
            "now": now,
        },
    )
    rowcount = await exec_rowcount(
        session,
        text(
            "INSERT INTO community_idempotency_requests "
            "(user_id, operation, idempotency_key, payload_hash, resource_type, "
            " resource_id, created_at, expires_at) "
            "VALUES (:user_id, :operation, :idempotency_key, :payload_hash, "
            " :resource_type, :resource_id, :now, :expires_at) "
            "ON CONFLICT (user_id, operation, idempotency_key) DO NOTHING"
        ),
        {
            "user_id": user_id,
            "operation": operation,
            "idempotency_key": idempotency_key,
            "payload_hash": payload_hash,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "now": now,
            "expires_at": now + timedelta(days=retention_days),
        },
    )
    return rowcount == 1


async def get_request(
    session: AsyncSession,
    *,
    user_id: UUID,
    operation: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT payload_hash, resource_type, resource_id FROM community_idempotency_requests "
            "WHERE user_id = :user_id AND operation = :operation "
            "  AND idempotency_key = :idempotency_key AND expires_at > :now"
        ),
        {
            "user_id": user_id,
            "operation": operation,
            "idempotency_key": idempotency_key,
            "now": datetime.now(UTC),
        },
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def delete_expired(session: AsyncSession, *, batch_size: int) -> int:
    """清理过期记录（§12.4：保留 7 天，按 batch 分批删除）。"""
    now = datetime.now(UTC)
    return await exec_rowcount(
        session,
        text(
            "DELETE FROM community_idempotency_requests "
            "WHERE ctid IN ("
            "  SELECT ctid FROM community_idempotency_requests "
            "  WHERE expires_at <= :now "
            "  ORDER BY expires_at "
            "  LIMIT :batch_size"
            ")"
        ),
        {"now": now, "batch_size": batch_size},
    )
