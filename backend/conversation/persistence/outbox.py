"""conversation_outbox 仓储（方案 §7.5）。

状态机：pending → processing → delivered；retry_wait → processing；dead_letter。
Publisher 的 claim、续租、投递后状态更新和 dead-letter 写入必须携带
lease_generation fencing 条件（附录 A.1：退避复用 retry.py 公式，cap 1800s）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount
from backend.memory.worker.retry import outbox_backoff_seconds

_INSERT_SQL = text(
    """
    INSERT INTO conversation.conversation_outbox (
        event_id, event_type, aggregate_type, aggregate_id, aggregate_version,
        idempotency_key, user_id, thread_id, turn_id, message_ids,
        source_checkpoint_id, trigger, topic_hints, graph_node_hints,
        status, attempt_count, next_attempt_at, lease_owner, lease_generation,
        lease_expires_at, last_error_code, created_at
    ) VALUES (
        :event_id, :event_type, :aggregate_type, :aggregate_id, :aggregate_version,
        :idempotency_key, :user_id, :thread_id, :turn_id, :message_ids,
        :source_checkpoint_id, :trigger, :topic_hints, :graph_node_hints,
        'pending', 0, :next_attempt_at, NULL, 0, NULL, NULL, :now
    )
    ON CONFLICT ON CONSTRAINT conversation_outbox_idempotency_key_key DO NOTHING
    """
)


async def insert_outbox(
    session: AsyncSession,
    *,
    event_id: UUID,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    aggregate_version: int,
    idempotency_key: str,
    user_id: UUID,
    thread_id: UUID,
    turn_id: UUID | None,
    message_ids: list[UUID],
    source_checkpoint_id: str | None,
    trigger: str | None,
    topic_hints: list[str],
    graph_node_hints: list[str],
) -> bool:
    """插入 Outbox 行（与消息/事件同一事务）；幂等冲突返回 False。"""
    now = datetime.now(UTC)
    return (
        await exec_rowcount(
            session,
            _INSERT_SQL,
            {
                "event_id": event_id,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "aggregate_version": aggregate_version,
                "idempotency_key": idempotency_key,
                "user_id": user_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "message_ids": [str(mid) for mid in message_ids],
                "source_checkpoint_id": source_checkpoint_id,
                "trigger": trigger,
                "topic_hints": topic_hints,
                "graph_node_hints": graph_node_hints,
                "next_attempt_at": now,
                "now": now,
            },
        )
    ) == 1


async def claim_outbox(
    session: AsyncSession,
    *,
    worker_id: str,
    lease_seconds: int,
    event_types: tuple[str, ...] | None = None,
    now: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """批量 claim 可投递 Outbox（pending/retry_wait 到点，或过期 processing 回收）。

    修复（评审 C5）：崩溃遗留的 processing（lease 已过期）必须可回收，
    否则 Memory Evidence 永久卡死。
    """
    now = now or datetime.now(UTC)
    params: dict[str, Any] = {"now": now, "limit": limit, "worker_id": worker_id}
    sql = (
        "SELECT * FROM conversation.conversation_outbox "
        "WHERE ("
        "  (status IN ('pending', 'retry_wait') AND next_attempt_at <= :now)"
        "  OR (status = 'processing' AND lease_expires_at IS NOT NULL AND lease_expires_at < :now)"
        ")"
    )
    if event_types:
        sql += " AND event_type = ANY(:event_types)"
        params["event_types"] = list(event_types)
    sql += " ORDER BY created_at LIMIT :limit FOR UPDATE SKIP LOCKED"
    result = await session.execute(text(sql), params)
    rows = [dict(r) for r in result.mappings()]
    for row in rows:
        generation = int(row["lease_generation"]) + 1
        await session.execute(
            text(
                "UPDATE conversation.conversation_outbox "
                "SET status = 'processing', lease_owner = :owner, "
                "    lease_generation = :generation, lease_expires_at = :expires, "
                "    attempt_count = attempt_count + 1 "
                "WHERE event_id = :event_id AND lease_generation = :current"
            ),
            {
                "owner": worker_id,
                "generation": generation,
                "expires": now + timedelta(seconds=lease_seconds),
                "event_id": row["event_id"],
                "current": int(row["lease_generation"]),
            },
        )
        row["status"] = "processing"
        row["lease_owner"] = worker_id
        row["lease_generation"] = generation
        row["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
        row["attempt_count"] = int(row["attempt_count"]) + 1
    return rows


async def get_outbox(session: AsyncSession, event_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM conversation.conversation_outbox WHERE event_id = :event_id"),
        {"event_id": event_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def mark_delivered(session: AsyncSession, event_id: UUID, *, worker_id: str) -> bool:
    """投递成功终态（fencing：仅当前 lease owner 可写）。"""
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.conversation_outbox "
                "SET status = 'delivered', delivered_at = :now, lease_owner = NULL, "
                "    lease_expires_at = NULL "
                "WHERE event_id = :event_id AND lease_owner = :owner AND status = 'processing'"
            ),
            {"event_id": event_id, "owner": worker_id, "now": datetime.now(UTC)},
        )
    ) == 1


async def mark_retry_wait(
    session: AsyncSession,
    event_id: UUID,
    *,
    worker_id: str,
    error_code: str,
    now: datetime | None = None,
    rng: Any = None,
) -> bool:
    """可重试失败：进入 retry_wait 并退避（附录 A.1 公式，cap 1800s）。"""
    now = now or datetime.now(UTC)
    import random as _random

    rng = rng or _random.Random()
    row = await get_outbox(session, event_id)
    if row is None or row["lease_owner"] != worker_id:
        return False
    backoff = outbox_backoff_seconds(int(row["attempt_count"]), rng=rng)
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.conversation_outbox "
                "SET status = 'retry_wait', next_attempt_at = :next_attempt_at, "
                "    last_error_code = :error_code, lease_owner = NULL, lease_expires_at = NULL "
                "WHERE event_id = :event_id AND lease_owner = :owner AND status = 'processing'"
            ),
            {
                "next_attempt_at": now + timedelta(seconds=backoff),
                "error_code": error_code,
                "event_id": event_id,
                "owner": worker_id,
            },
        )
    ) == 1


async def mark_dead_letter(
    session: AsyncSession,
    event_id: UUID,
    *,
    worker_id: str,
    error_code: str,
) -> bool:
    """永久失败：dead_letter（4xx 契约/权限直接死信不排队，附录 A.1）。"""
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.conversation_outbox "
                "SET status = 'dead_letter', last_error_code = :error_code, "
                "    lease_owner = NULL, lease_expires_at = NULL "
                "WHERE event_id = :event_id AND lease_owner = :owner AND status = 'processing'"
            ),
            {"error_code": error_code, "event_id": event_id, "owner": worker_id},
        )
    ) == 1


async def count_outbox_by_status(session: AsyncSession, status: str) -> int:
    """指标：按状态统计 Outbox 数量（§23.2）。"""
    result = await session.execute(
        text("SELECT COUNT(*) FROM conversation.conversation_outbox WHERE status = :status"),
        {"status": status},
    )
    return int(result.scalar_one())


async def oldest_pending_age_seconds(session: AsyncSession) -> float | None:
    """指标：最老 pending/retry_wait 事件年龄（§23.2）。"""
    result = await session.execute(
        text(
            "SELECT EXTRACT(EPOCH FROM (now() - MIN(created_at)) "
            "FROM conversation.conversation_outbox "
            "WHERE status IN ('pending', 'retry_wait')"
        )
    )
    value = result.scalar_one_or_none()
    return float(value) if value is not None else None


async def list_outbox_by_thread(
    session: AsyncSession, thread_id: UUID, *, deletion_generation: int
) -> list[dict[str, Any]]:
    """delete_thread Job 等待语义（R3/S3）：检查本 generation 全部 deletion Outbox。"""
    result = await session.execute(
        text(
            "SELECT * FROM conversation.conversation_outbox "
            "WHERE thread_id = :thread_id AND event_type = 'memory.source_deleted' "
            "  AND aggregate_id = :generation "
            "ORDER BY created_at"
        ),
        {"thread_id": thread_id, "generation": str(deletion_generation)},
    )
    return [dict(row) for row in result.mappings()]


async def serialize_outbox_row(row: dict[str, Any]) -> dict[str, Any]:
    """Outbox 行 JSON 字段反序列化（message_ids 等）。"""
    out = dict(row)
    out["message_ids"] = [str(mid) for mid in row.get("message_ids") or []]
    return out
