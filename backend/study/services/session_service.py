"""Study Session 领域服务（§12.3/§12.5/D23/D24/D28/§13.2）。

- Session 只能由 task start/launch 创建或复用（D23，无自习 Session）；
- heartbeat：单调 seq 幂等/乱序/过快判定（session_timing 纯函数）；
- finish：结算最后一段有效区间，把活跃秒写入 daily_stats（按结束时刻的
  计划时区归属自然日，§7.10 增量统计）；
- launch：稳定响应骨架 + conversation_status=pending（thread 回填 Phase 3）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.study.contracts.errors import (
    StudyRateLimitedError,
    StudySessionConflictError,
)
from backend.study.persistence import repositories as repo
from backend.study.services.session_timing import heartbeat_decision


async def create_or_reuse_session(
    session: AsyncSession,
    *,
    user_id: UUID,
    task_row: dict[str, Any],
    launch: bool,
) -> tuple[dict[str, Any], bool]:
    """为任务创建或复用 active Session（§12.3/D23）。返回 (row, created)。"""
    task_id = UUID(str(task_row["task_id"]))
    existing = await repo.find_active_session_for_task(session, task_id=task_id)
    if existing is not None:
        session_id = UUID(str(existing["session_id"]))
        if launch and existing["conversation_status"] == "not_requested":
            await _mark_conversation_pending(session, session_id=session_id)
            refreshed = await repo.get_session_row(session, user_id=user_id, session_id=session_id)
            assert refreshed is not None
            return refreshed, False
        return existing, False
    session_id = uuid4()
    await repo.insert_session(
        session,
        session_id=session_id,
        user_id=user_id,
        task_id=task_id,
        conversation_status="pending" if launch else "not_requested",
        conversation_create_request_id=str(session_id) if launch else None,
    )
    created = await repo.get_session_row(session, user_id=user_id, session_id=session_id)
    assert created is not None
    return created, True


async def _mark_conversation_pending(session: AsyncSession, *, session_id: UUID) -> None:
    await session.execute(
        text(
            "UPDATE study_sessions SET conversation_status = 'pending', "
            "conversation_create_request_id = COALESCE(conversation_create_request_id, :rid) "
            "WHERE session_id = :session_id"
        ),
        {"rid": str(session_id), "session_id": session_id},
    )


async def heartbeat(
    session: AsyncSession,
    *,
    session_row: dict[str, Any],
    seq: int,
    now: datetime,
    min_interval_seconds: int,
    idle_timeout_seconds: int,
) -> dict[str, Any]:
    """§12.5：判定并落库一次 heartbeat。"""
    if session_row["status"] != "active":
        raise StudySessionConflictError("Session 已结束，不能继续 heartbeat")
    decision, added = heartbeat_decision(
        seq=seq,
        last_seq=int(session_row["last_heartbeat_seq"]),
        now=now,
        last_heartbeat_at=session_row["last_heartbeat_at"],
        min_interval_seconds=min_interval_seconds,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    if decision == "conflict":
        raise StudySessionConflictError("heartbeat seq 乱序（小于已确认值）")
    if decision == "replay":
        return session_row
    if decision == "too_fast":
        raise StudyRateLimitedError(
            "heartbeat 过快（小于最小有效间隔）", retry_after=max(1, min_interval_seconds)
        )
    updated = await repo.update_session_heartbeat(
        session,
        session_id=UUID(str(session_row["session_id"])),
        user_id=UUID(str(session_row["user_id"])),
        seq=seq,
        now=now,
        added_seconds=added,
    )
    if not updated:
        # C14：CAS rowcount=0（并发更高 seq 已落库）→ 重读判定，不再静默成功
        raise StudySessionConflictError("heartbeat 已被更新序列占用（并发冲突）")
    refreshed = await repo.get_session_row(
        session,
        user_id=UUID(str(session_row["user_id"])),
        session_id=UUID(str(session_row["session_id"])),
    )
    assert refreshed is not None
    return refreshed


def settle_seconds(session_row: dict[str, Any], now: datetime, idle_timeout: int) -> int:
    """结算最后一段有效区间（§13.2 规则 5/6：空闲上限截断）。"""
    last = session_row["last_heartbeat_at"]
    if last is None:
        return 0
    gap = (now - last).total_seconds()
    if gap <= 0:
        return 0
    return int(min(gap, idle_timeout))


async def finish_session(
    session: AsyncSession,
    *,
    session_row: dict[str, Any],
    now: datetime,
    idle_timeout_seconds: int,
    plan_timezone: str,
    abandoned: bool = False,
) -> dict[str, Any]:
    """结束 Session 并把活跃秒写入 daily_stats（§7.10/§13.3）。"""
    if session_row["status"] != "active":
        raise StudySessionConflictError("Session 已结束，不能重复 finish")
    added = settle_seconds(session_row, now, idle_timeout_seconds)
    updated = await repo.update_session_finish(
        session,
        session_id=UUID(str(session_row["session_id"])),
        user_id=UUID(str(session_row["user_id"])),
        new_status="abandoned" if abandoned else "completed",
        now=now,
        added_seconds=added,
    )
    if not updated:
        raise StudySessionConflictError("Session 状态已变化，finish 失败")
    local_date = now.astimezone(ZoneInfo(plan_timezone)).date()
    await repo.upsert_daily_stats_add_activity(
        session,
        user_id=UUID(str(session_row["user_id"])),
        local_date=local_date,
        active_seconds=added,
        session_count=1,
    )
    refreshed = await repo.get_session_row(
        session,
        user_id=UUID(str(session_row["user_id"])),
        session_id=UUID(str(session_row["session_id"])),
    )
    assert refreshed is not None
    return refreshed
