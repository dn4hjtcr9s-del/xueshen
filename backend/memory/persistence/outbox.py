"""Outbox 仓储（规格 §13.11 / §13.12 / §14.4）。

事件与 commit 同一事务写入；Consumer 以 (event_type, target) 路由，
目标侧使用 inbox/唯一幂等键。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount

#: event_type -> 第一版启用 target（§14.4）
EVENT_TARGETS: dict[str, list[str]] = {
    "memory.changed": ["summary_projection", "internal_event_log"],
    "learner.updated": ["internal_event_log"],
    "memory.deleted": ["summary_projection", "user_notification", "internal_event_log"],
    "memory.restored": ["summary_projection", "user_notification", "internal_event_log"],
    "review_candidate.created": ["user_notification", "internal_event_log"],
    "review_candidate.resolved": ["user_notification", "internal_event_log"],
    "graph_state.changed": ["user_notification", "internal_event_log"],
    "graph_state.explanation_available": ["user_notification", "internal_event_log"],
    "account_memory.purge_requested": ["internal_event_log"],
}


async def insert_event(
    session: AsyncSession,
    *,
    outbox_id: UUID,
    operation_id: UUID | None,
    user_id: UUID,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    payload: dict[str, Any],
) -> bool:
    """写 Outbox 主行并为每个启用 target 创建 delivery。幂等冲突返回 False。"""
    rowcount = await exec_rowcount(
        session,
        text(
            """
            INSERT INTO memory_outbox (
                outbox_id, operation_id, user_id, event_type,
                aggregate_type, aggregate_id, aggregate_version, payload
            ) VALUES (
                :outbox_id, :operation_id, :user_id, :event_type,
                :aggregate_type, :aggregate_id, :aggregate_version,
                CAST(:payload AS jsonb)
            )
            ON CONFLICT ON CONSTRAINT uq_memory_outbox_event DO NOTHING
            """
        ),
        {
            "outbox_id": outbox_id,
            "operation_id": operation_id,
            "user_id": user_id,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "aggregate_version": aggregate_version,
            "payload": json.dumps(payload, ensure_ascii=False),
        },
    )
    if rowcount != 1:
        return False
    for target in EVENT_TARGETS[event_type]:
        await session.execute(
            text(
                """
                INSERT INTO memory_outbox_deliveries (
                    delivery_id, outbox_id, target, idempotency_key
                ) VALUES (
                    :delivery_id, :outbox_id, :target, :idempotency_key
                )
                ON CONFLICT (outbox_id, target) DO NOTHING
                """
            ),
            {
                "delivery_id": uuid4(),
                "outbox_id": outbox_id,
                "target": target,
                "idempotency_key": f"{event_type}:{aggregate_id}:{aggregate_version}:{target}",
            },
        )
    return True


async def claim_batch(
    session: AsyncSession, *, worker_id: str, lease_seconds: int, batch_size: int
) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT outbox_id FROM memory_outbox
                    WHERE status IN ('pending', 'retry_wait') AND next_run_at <= now()
                    ORDER BY created_at ASC
                    LIMIT :batch_size
                    FOR UPDATE SKIP LOCKED
                    """
                ),
                {"batch_size": batch_size},
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []
    await session.execute(
        text(
            """
            UPDATE memory_outbox
            SET status = 'publishing', locked_by = :worker_id,
                lease_expires_at = :lease_expires, attempt_count = attempt_count + 1
            WHERE outbox_id = ANY(:ids)
            """
        ),
        {
            "worker_id": worker_id,
            "lease_expires": datetime.now(UTC) + timedelta(seconds=lease_seconds),
            "ids": list(rows),
        },
    )
    result = await session.execute(
        text("SELECT * FROM memory_outbox WHERE outbox_id = ANY(:ids)"), {"ids": list(rows)}
    )
    return [dict(r) for r in result.mappings().all()]


async def list_deliveries(session: AsyncSession, *, outbox_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        text("SELECT * FROM memory_outbox_deliveries WHERE outbox_id = :outbox_id ORDER BY target"),
        {"outbox_id": outbox_id},
    )
    return [dict(r) for r in result.mappings().all()]


async def mark_delivery(
    session: AsyncSession,
    *,
    delivery_id: UUID,
    status: str,
    last_error: dict[str, Any] | None = None,
) -> None:
    await session.execute(
        text(
            """
            UPDATE memory_outbox_deliveries
            SET status = :status,
                completed_at = CASE WHEN :status = 'succeeded' THEN now() ELSE NULL END,
                attempt_count = attempt_count + 1,
                last_error = CAST(:last_error AS jsonb)
            WHERE delivery_id = :delivery_id
            """
        ),
        {
            "delivery_id": delivery_id,
            "status": status,
            "last_error": (
                json.dumps(last_error, ensure_ascii=False) if last_error is not None else None
            ),
        },
    )


async def finalize_outbox(session: AsyncSession, *, outbox_id: UUID) -> None:
    """全部启用 target 成功后主行 published；任一 dead_letter 则主行 dead_letter。"""
    await session.execute(
        text(
            """
            UPDATE memory_outbox o
            SET status = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM memory_outbox_deliveries d
                        WHERE d.outbox_id = o.outbox_id AND d.status = 'dead_letter'
                    ) THEN 'dead_letter'
                    WHEN EXISTS (
                        SELECT 1 FROM memory_outbox_deliveries d
                        WHERE d.outbox_id = o.outbox_id
                          AND d.status IN ('pending', 'retry_wait')
                    ) THEN 'retry_wait'
                    ELSE 'published'
                END,
                published_at = CASE
                    WHEN NOT EXISTS (
                        SELECT 1 FROM memory_outbox_deliveries d
                        WHERE d.outbox_id = o.outbox_id AND d.status != 'succeeded'
                    ) THEN now()
                    ELSE NULL
                END,
                locked_by = NULL, lease_expires_at = NULL,
                next_run_at = now()
            WHERE o.outbox_id = :outbox_id
            """
        ),
        {"outbox_id": outbox_id},
    )


async def get_status(session: AsyncSession, *, outbox_id: UUID) -> str | None:
    result = await session.execute(
        text("SELECT status FROM memory_outbox WHERE outbox_id = :outbox_id"),
        {"outbox_id": outbox_id},
    )
    value = result.scalar_one_or_none()
    return str(value) if value is not None else None


async def reschedule_outbox(
    session: AsyncSession, *, outbox_id: UUID, next_run_at: datetime
) -> None:
    await session.execute(
        text(
            """
            UPDATE memory_outbox
            SET status = 'retry_wait', next_run_at = :next_run_at,
                locked_by = NULL, lease_expires_at = NULL
            WHERE outbox_id = :outbox_id
            """
        ),
        {"outbox_id": outbox_id, "next_run_at": next_run_at},
    )


async def recover_expired_leases(session: AsyncSession) -> int:
    rowcount = await exec_rowcount(
        session,
        text(
            """
            UPDATE memory_outbox
            SET status = 'retry_wait', next_run_at = now(),
                locked_by = NULL, lease_expires_at = NULL
            WHERE status = 'publishing' AND lease_expires_at < now()
            """
        ),
    )
    return rowcount


async def insert_internal_event_log(
    session: AsyncSession,
    *,
    event_log_id: UUID,
    outbox_id: UUID,
    event_type: str,
    idempotency_key: str,
    user_id: UUID,
    payload: dict[str, Any],
) -> bool:
    rowcount = await exec_rowcount(
        session,
        text(
            """
            INSERT INTO memory_internal_event_log (
                event_log_id, outbox_id, event_type, idempotency_key, user_id, payload
            ) VALUES (
                :event_log_id, :outbox_id, :event_type, :idempotency_key, :user_id,
                CAST(:payload AS jsonb)
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            """
        ),
        {
            "event_log_id": event_log_id,
            "outbox_id": outbox_id,
            "event_type": event_type,
            "idempotency_key": idempotency_key,
            "user_id": user_id,
            "payload": json.dumps(payload, ensure_ascii=False),
        },
    )
    return rowcount == 1


async def oldest_pending_seconds(session: AsyncSession) -> float | None:
    result = await session.execute(
        text(
            "SELECT EXTRACT(EPOCH FROM (now() - MIN(created_at))) "
            "FROM memory_outbox WHERE status IN ('pending', 'retry_wait')"
        )
    )
    value = result.scalar()
    return float(value) if value is not None else None
