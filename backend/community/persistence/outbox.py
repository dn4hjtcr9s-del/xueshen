"""community_outbox 仓储（方案 §7.5，v1.6 列集冻结）。

- 与业务写入同事务插入；idempotency_key UNIQUE + ON CONFLICT DO NOTHING
  天然去重（D32：键 = community:{event_type}:{aggregate_id}）；
- claim/写回 fencing 语义对齐 Conversation Outbox（outbox.py:115-217）：
  claim CAS 携带 lease_generation，写回以 lease_owner + status='processing'
  双条件防跨租约写入（§7.5 差异点：Community 用 payload jsonb，不照搬类型化列）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount


async def insert_event(
    session: AsyncSession,
    *,
    event_id: UUID,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    user_id: UUID,
    payload: dict[str, Any],
    idempotency_key: str,
) -> bool:
    """插入 outbox 事件；同 idempotency_key 已存在返回 False（幂等入队）。"""
    return (
        await exec_rowcount(
            session,
            text(
                "INSERT INTO community_outbox "
                "(event_id, event_type, aggregate_type, aggregate_id, user_id, payload, "
                " idempotency_key) "
                "VALUES (:event_id, :event_type, :aggregate_type, :aggregate_id, :user_id, "
                " CAST(:payload AS jsonb), :idempotency_key) "
                "ON CONFLICT (idempotency_key) DO NOTHING"
            ),
            {
                "event_id": event_id,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "user_id": user_id,
                "payload": json.dumps(payload, ensure_ascii=False),
                "idempotency_key": idempotency_key,
            },
        )
        == 1
    )


async def claim_events(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    batch_size: int,
    allowed_event_types: tuple[str, ...],
) -> list[dict[str, Any]]:
    """claim pending/retry_wait（含过期 lease 的 processing）事件（§12.1）。

    CAS：仅当 lease_generation 未被他人递增时写入新 lease（对齐
    conversation outbox.py:115-131 语义）。
    """
    now = datetime.now(UTC)
    rows = (
        (
            await session.execute(
                text(
                    "SELECT * FROM community_outbox "
                    "WHERE (event_type = ANY(:event_types)) AND ("
                    "  (status IN ('pending', 'retry_wait') AND next_attempt_at <= :now) "
                    "  OR (status = 'processing' AND lease_expires_at IS NOT NULL "
                    "     AND lease_expires_at < :now)) "
                    "ORDER BY created_at LIMIT :batch_size "
                    "FOR UPDATE SKIP LOCKED"
                ),
                {
                    "event_types": list(allowed_event_types),
                    "now": now,
                    "batch_size": batch_size,
                },
            )
        )
        .mappings()
        .all()
    )
    claimed: list[dict[str, Any]] = []
    for row in rows:
        generation = int(row["lease_generation"]) + 1
        updated = await session.execute(
            text(
                "UPDATE community_outbox "
                "SET status = 'processing', lease_owner = :owner, "
                "    lease_generation = :generation, lease_expires_at = :expires, "
                "    updated_at = :now "
                "WHERE event_id = :event_id AND lease_generation = :current"
            ),
            {
                "owner": worker_id,
                "generation": generation,
                "expires": now + timedelta_seconds(lease_seconds),
                "now": now,
                "event_id": row["event_id"],
                "current": int(row["lease_generation"]),
            },
        )
        if isinstance(updated, CursorResult) and updated.rowcount == 1:
            item = dict(row)
            item["lease_generation"] = generation
            item["status"] = "processing"
            claimed.append(item)
    return claimed


def timedelta_seconds(seconds: int) -> Any:
    from datetime import timedelta

    return timedelta(seconds=seconds)


async def mark_delivered(
    session: AsyncSession,
    event_id: UUID,
    *,
    worker_id: str,
    delivery_result: str | None = None,
) -> bool:
    """写回 delivered（fencing：lease_owner + status='processing' 双条件）。"""
    now = datetime.now(UTC)
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE community_outbox SET status = 'delivered', delivered_at = :now, "
                "    lease_owner = NULL, delivery_result = :delivery_result, updated_at = :now "
                "WHERE event_id = :event_id AND lease_owner = :owner AND status = 'processing'"
            ),
            {
                "event_id": event_id,
                "owner": worker_id,
                "now": now,
                "delivery_result": delivery_result,
            },
        )
        == 1
    )


async def mark_retry_wait(
    session: AsyncSession,
    event_id: UUID,
    *,
    worker_id: str,
    error_code: str,
    next_attempt_at: Any,
) -> bool:
    now = datetime.now(UTC)
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE community_outbox SET status = 'retry_wait', "
                "    next_attempt_at = :next_attempt_at, attempt_count = attempt_count + 1, "
                "    last_error_code = :error_code, lease_owner = NULL, updated_at = :now "
                "WHERE event_id = :event_id AND lease_owner = :owner AND status = 'processing'"
            ),
            {
                "event_id": event_id,
                "owner": worker_id,
                "next_attempt_at": next_attempt_at,
                "error_code": error_code,
                "now": now,
            },
        )
        == 1
    )


async def mark_dead_letter(
    session: AsyncSession,
    event_id: UUID,
    *,
    worker_id: str,
    error_code: str,
) -> bool:
    now = datetime.now(UTC)
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE community_outbox SET status = 'dead_letter', "
                "    last_error_code = :error_code, lease_owner = NULL, updated_at = :now "
                "WHERE event_id = :event_id AND lease_owner = :owner AND status = 'processing'"
            ),
            {"event_id": event_id, "owner": worker_id, "error_code": error_code, "now": now},
        )
        == 1
    )
