"""conversation_turns 仓储（方案 §7.2 / §5.4 / §1.5）。

核心语义：
- claim：accepted 且 next_attempt_at<=now()，或 running/cancelling 且 lease 过期（回收）；
- 每次成功 claim 同一事务原子递增 lease_generation 与 attempt_count；
- last_event_sequence 是唯一事件序号分配器：事务锁 Turn 行后原子 +1（§7.4 / Q8）；
- 取消原子分支（R2）：accepted 直接转 cancelled；running 转 cancelling；
- 删除事务中 accepted 直接取消、running 置 cancelling（R4）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.contracts.domain import TurnRow
from backend.memory.persistence.database import exec_rowcount

_INSERT_SQL = text(
    """
    INSERT INTO conversation.conversation_turns (
        turn_id, thread_id, user_id, client_request_id, request_id, run_id,
        user_message_id, status, lease_owner, lease_generation, lease_expires_at,
        attempt_count, next_attempt_at, expected_thread_version, graph_thread_id,
        memory_trigger, memory_submission_status, last_event_sequence,
        degraded_flags, created_at, updated_at
    ) VALUES (
        :turn_id, :thread_id, :user_id, :client_request_id, :request_id, :run_id,
        :user_message_id, 'accepted', NULL, 0, NULL,
        0, :next_attempt_at, :expected_thread_version, :graph_thread_id,
        :memory_trigger, 'not_required', 0,
        '{}', :now, :now
    )
    """
)


async def insert_turn(
    session: AsyncSession,
    *,
    turn_id: UUID,
    thread_id: UUID,
    user_id: UUID,
    client_request_id: str,
    request_id: str,
    run_id: str,
    user_message_id: UUID,
    expected_thread_version: int,
    graph_thread_id: str,
    memory_trigger: str = "turn_boundary",
    next_attempt_at: datetime | None = None,
) -> bool:
    """插入 Turn（status=accepted，next_attempt_at=created_at 立即可 claim，附录 A.2）。"""
    now = datetime.now(UTC)
    return (
        await exec_rowcount(
            session,
            _INSERT_SQL,
            {
                "turn_id": turn_id,
                "thread_id": thread_id,
                "user_id": user_id,
                "client_request_id": client_request_id,
                "request_id": request_id,
                "run_id": run_id,
                "user_message_id": user_message_id,
                "next_attempt_at": next_attempt_at or now,
                "expected_thread_version": expected_thread_version,
                "graph_thread_id": graph_thread_id,
                "memory_trigger": memory_trigger,
                "now": now,
            },
        )
    ) == 1


async def get_turn(
    session: AsyncSession, turn_id: UUID, *, for_update: bool = False
) -> dict[str, Any] | None:
    sql = "SELECT * FROM conversation.conversation_turns WHERE turn_id = :turn_id"
    if for_update:
        sql += " FOR UPDATE"
    result = await session.execute(text(sql), {"turn_id": turn_id})
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_turn_by_client_request(
    session: AsyncSession, thread_id: UUID, client_request_id: str
) -> dict[str, Any] | None:
    """幂等命中：同线程同 client_request_id 返回既有 Turn（§17.3）。"""
    result = await session.execute(
        text(
            "SELECT * FROM conversation.conversation_turns "
            "WHERE thread_id = :thread_id AND client_request_id = :client_request_id"
        ),
        {"thread_id": thread_id, "client_request_id": client_request_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def get_active_turn(
    session: AsyncSession, thread_id: UUID, *, for_update: bool = False
) -> dict[str, Any] | None:
    """同线程活动 Turn（accepted/running/cancelling）——业务串行约束（§5.4）。"""
    sql = (
        "SELECT * FROM conversation.conversation_turns "
        "WHERE thread_id = :thread_id AND status IN ('accepted', 'running', 'cancelling')"
    )
    if for_update:
        sql += " FOR UPDATE"
    result = await session.execute(text(sql), {"thread_id": thread_id})
    row = result.mappings().first()
    return dict(row) if row is not None else None


def turn_row_from_row(row: dict[str, Any]) -> TurnRow:
    return TurnRow(
        turn_id=row["turn_id"],
        thread_id=row["thread_id"],
        user_id=row["user_id"],
        client_request_id=row["client_request_id"],
        request_id=row["request_id"],
        run_id=row["run_id"],
        user_message_id=row["user_message_id"],
        assistant_message_id=row.get("assistant_message_id"),
        status=row["status"],
        lease_owner=row.get("lease_owner"),
        lease_generation=row.get("lease_generation", 0),
        lease_expires_at=row.get("lease_expires_at"),
        attempt_count=row.get("attempt_count", 0),
        next_attempt_at=row["next_attempt_at"],
        expected_thread_version=row["expected_thread_version"],
        graph_thread_id=row.get("graph_thread_id"),
        graph_checkpoint_id=row.get("graph_checkpoint_id"),
        source_checkpoint_id=row.get("source_checkpoint_id"),
        plan_revision=row.get("plan_revision", 0),
        memory_trigger=row["memory_trigger"],
        memory_submission_status=row["memory_submission_status"],
        memory_operation_id=row.get("memory_operation_id"),
        last_event_sequence=row["last_event_sequence"],
        degraded_flags=list(row.get("degraded_flags") or []),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_CLAIMABLE_SQL = text(
    """
    SELECT * FROM conversation.conversation_turns
    WHERE turn_id = :turn_id
      AND (
        (status = 'accepted' AND next_attempt_at <= :now)
        OR
        (status IN ('running', 'cancelling')
         AND (lease_expires_at IS NULL OR lease_expires_at < :now))
      )
    FOR UPDATE SKIP LOCKED
    """
)


async def try_claim_turn(
    session: AsyncSession,
    turn_id: UUID,
    *,
    worker_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """原子 claim：可 claim 条件 + 递增 lease_generation/attempt_count（§5.4）。"""
    now = now or datetime.now(UTC)
    row = await _lock_claimable(session, turn_id, now=now)
    if row is None:
        return None
    next_generation = int(row["lease_generation"]) + 1
    new_attempt = int(row["attempt_count"]) + 1
    await session.execute(
        text(
            "UPDATE conversation.conversation_turns "
            "SET lease_owner = :owner, lease_generation = :generation, "
            "    lease_expires_at = :expires, attempt_count = :attempt, "
            "    status = CASE WHEN status = 'accepted' THEN 'running' ELSE status END, "
            "    next_attempt_at = :next_attempt_at, updated_at = :now "
            "WHERE turn_id = :turn_id AND lease_generation = :current_generation"
        ),
        {
            "owner": worker_id,
            "generation": next_generation,
            "expires": now + timedelta(seconds=lease_seconds),
            "attempt": new_attempt,
            "next_attempt_at": _reclaim_backoff(new_attempt, now),
            "now": now,
            "turn_id": turn_id,
            "current_generation": int(row["lease_generation"]),
        },
    )
    row["lease_owner"] = worker_id
    row["lease_generation"] = next_generation
    row["lease_expires_at"] = now + timedelta(seconds=lease_seconds)
    row["attempt_count"] = new_attempt
    row["next_attempt_at"] = _reclaim_backoff(new_attempt, now)
    row["status"] = "cancelling" if row["status"] == "cancelling" else "running"
    return row


async def _lock_claimable(
    session: AsyncSession, turn_id: UUID, *, now: datetime
) -> dict[str, Any] | None:
    """在事务内锁定可 claim 的 Turn 行（SKIP LOCKED 防并发重复 claim）。"""
    result = await session.execute(_CLAIMABLE_SQL, {"turn_id": turn_id, "now": now})
    row = result.mappings().first()
    return dict(row) if row is not None else None


def _reclaim_backoff(attempt: int, now: datetime) -> datetime:
    """Turn claim 退避（附录 A.2）：cap 60s，无 jitter（attempt 从 1 开始）。"""
    from backend.memory.worker.retry import TASK_BACKOFF_BASE_SECONDS

    base = min(TASK_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)), 60)
    return now + timedelta(seconds=base)


async def renew_lease(
    session: AsyncSession,
    turn_id: UUID,
    *,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    """Worker 心跳续租：必须携带 lease_generation + owner fencing 条件（§5.4）。"""
    row = await get_turn(session, turn_id)
    if row is None or row["lease_owner"] != worker_id:
        return False
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.conversation_turns "
                "SET lease_expires_at = :expires, updated_at = :now "
                "WHERE turn_id = :turn_id AND lease_owner = :owner "
                "  AND lease_generation = :generation"
            ),
            {
                "expires": datetime.now(UTC) + timedelta(seconds=lease_seconds),
                "now": datetime.now(UTC),
                "turn_id": turn_id,
                "owner": worker_id,
                "generation": row["lease_generation"],
            },
        )
    ) == 1


async def renew_lease_via_factory(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    turn_id: UUID,
    worker_id: str,
    lease_seconds: int,
) -> bool:
    """心跳续租入口：自建事务调用 renew_lease（C4 评审：graph_worker 心跳用）。"""
    async with session_factory() as session:
        async with session.begin():
            return await renew_lease(
                session, turn_id, worker_id=worker_id, lease_seconds=lease_seconds
            )


async def cancel_accepted_turn(
    session: AsyncSession, turn_id: UUID, *, now: datetime | None = None
) -> bool:
    """取消原子分支（R2）：accepted 直接转 cancelled，不等待 Worker。"""
    now = now or datetime.now(UTC)
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.conversation_turns "
                "SET status = 'cancelled', updated_at = :now "
                "WHERE turn_id = :turn_id AND status = 'accepted'"
            ),
            {"turn_id": turn_id, "now": now},
        )
    ) == 1


async def mark_cancelling(
    session: AsyncSession, turn_id: UUID, *, now: datetime | None = None
) -> bool:
    """取消原子分支（R2）：running 转 cancelling。"""
    now = now or datetime.now(UTC)
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.conversation_turns "
                "SET status = 'cancelling', updated_at = :now "
                "WHERE turn_id = :turn_id AND status = 'running'"
            ),
            {"turn_id": turn_id, "now": now},
        )
    ) == 1


async def write_terminal_cancelled(
    session: AsyncSession,
    turn_id: UUID,
    *,
    worker_id: str | None = None,
) -> bool:
    """cancelling Turn 终态写入：只能转 cancelled，不得恢复回答（R2）。

    携带 lease fencing（worker 上下文）或无条件（回收者）写终态。
    """
    sql = (
        "UPDATE conversation.conversation_turns "
        "SET status = 'cancelled', updated_at = :now "
        "WHERE turn_id = :turn_id AND status = 'cancelling'"
    )
    params: dict[str, Any] = {"turn_id": turn_id, "now": datetime.now(UTC)}
    if worker_id is not None:
        sql += " AND lease_owner = :owner"
        params["owner"] = worker_id
    return (await exec_rowcount(session, text(sql), params)) == 1


async def write_cancelled_running(
    session: AsyncSession,
    turn_id: UUID,
    *,
    worker_id: str | None = None,
) -> bool:
    """R4（删除协调）：Thread 已 deleting 时，running Turn 直接转 cancelled。

    与 write_terminal_cancelled 的区别：状态是 running（finalize 入口时刻），
    不写完成消息/Evidence（评审 C4：删除后不得重新写入完成副作用）。
    """
    sql = (
        "UPDATE conversation.conversation_turns "
        "SET status = 'cancelled', updated_at = :now "
        "WHERE turn_id = :turn_id AND status = 'running'"
    )
    params: dict[str, Any] = {"turn_id": turn_id, "now": datetime.now(UTC)}
    if worker_id is not None:
        sql += " AND lease_owner = :owner"
        params["owner"] = worker_id
    return (await exec_rowcount(session, text(sql), params)) == 1


async def write_terminal_failed(
    session: AsyncSession, turn_id: UUID, *, now: datetime | None = None
) -> bool:
    """attempt 超限/致命失败终态（§5.4：CONVERSATION_TURN_MAX_ATTEMPTS=3）。"""
    now = now or datetime.now(UTC)
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.conversation_turns "
                "SET status = 'failed', updated_at = :now "
                "WHERE turn_id = :turn_id AND status IN ('running', 'accepted')"
            ),
            {"turn_id": turn_id, "now": now},
        )
    ) == 1


async def allocate_event_sequence(session: AsyncSession, turn_id: UUID) -> int:
    """Turn Event 序号分配（§7.4 / Q8）：锁行后原子 +1 返回新值。"""
    result = await session.execute(
        text(
            "UPDATE conversation.conversation_turns "
            "SET last_event_sequence = last_event_sequence + 1, updated_at = :now "
            "WHERE turn_id = :turn_id RETURNING last_event_sequence"
        ),
        {"turn_id": turn_id, "now": datetime.now(UTC)},
    )
    row = result.mappings().first()
    if row is None:
        raise ValueError(f"Turn 不存在或已删除: {turn_id}")
    return int(row["last_event_sequence"])


async def update_memory_submission(
    session: AsyncSession,
    turn_id: UUID,
    *,
    status: str,
    operation_id: UUID | None = None,
) -> bool:
    """Publisher 更新 memory_submission 字段（§7.5 最小写范围 #2）。"""
    return (
        await exec_rowcount(
            session,
            text(
                "UPDATE conversation.conversation_turns "
                "SET memory_submission_status = :status, memory_operation_id = :op_id, "
                "    updated_at = :now "
                "WHERE turn_id = :turn_id"
            ),
            {
                "status": status,
                "op_id": operation_id,
                "now": datetime.now(UTC),
                "turn_id": turn_id,
            },
        )
    ) == 1


async def update_graph_checkpoint_id(
    session: AsyncSession, turn_id: UUID, *, checkpoint_id: str
) -> None:
    """记录最新 graph checkpoint id（仅观测/排障，恢复不按 id 定点，附录 A.3）。"""
    await session.execute(
        text(
            "UPDATE conversation.conversation_turns "
            "SET graph_checkpoint_id = :checkpoint_id, updated_at = :now "
            "WHERE turn_id = :turn_id"
        ),
        {"checkpoint_id": checkpoint_id, "turn_id": turn_id, "now": datetime.now(UTC)},
    )


async def list_events(session: AsyncSession, turn_id: UUID) -> list[dict[str, Any]]:
    """按 sequence 正序返回 Turn 事件（SSE 重放，§17.5）。"""
    result = await session.execute(
        text(
            "SELECT * FROM conversation.conversation_turn_events "
            "WHERE turn_id = :turn_id ORDER BY sequence"
        ),
        {"turn_id": turn_id},
    )
    return [dict(row) for row in result.mappings()]


async def earliest_event_sequence(session: AsyncSession, turn_id: UUID) -> int | None:
    """该 Turn 最早保留事件序号（Last-Event-ID 过期判定，§1.5 R1）。"""
    result = await session.execute(
        text(
            "SELECT MIN(sequence) FROM conversation.conversation_turn_events "
            "WHERE turn_id = :turn_id"
        ),
        {"turn_id": turn_id},
    )
    value = result.scalar_one_or_none()
    return int(value) if value is not None else None
