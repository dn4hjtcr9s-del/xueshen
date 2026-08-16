"""Study 任务领域服务（§12.3/D11/D13/D27/D28）。

- 状态转移严格走冻结矩阵（task_state 纯函数）；
- complete/skip 先结算活跃 Session（§12.3 矩阵说明）；
- complete 的 completion_source 固定 manual（D27），并写入任务事件与
  daily_stats.completed_task_count；
- reschedule 仅 pending：日期边界（不早于今天/不晚于 target_date/非休息日）
  + 目标日负载碰撞 → STUDY_SCHEDULE_CONFLICT（§12.3）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from backend.study.contracts.errors import (
    StudyInvalidTaskTransitionError,
    StudyScheduleConflictError,
    StudyTaskNotFoundError,
    StudyTaskVersionConflictError,
)
from backend.study.persistence import repositories as repo
from backend.study.services import task_state
from backend.study.services.session_service import finish_session


async def get_task(session: AsyncSession, *, user_id: UUID, task_id: UUID) -> dict[str, Any]:
    row = await repo.get_task_row(session, user_id=user_id, task_id=task_id)
    if row is None:
        raise StudyTaskNotFoundError("任务不存在或不属于当前用户")
    return row


async def _settle_active_session(
    session: AsyncSession,
    *,
    task_id: UUID,
    now: datetime,
    idle_timeout: int,
    plan_timezone: str,
    abandoned: bool,
) -> None:
    active = await repo.find_active_session_for_task(session, task_id=task_id)
    if active is not None:
        await finish_session(
            session,
            session_row=active,
            now=now,
            idle_timeout_seconds=idle_timeout,
            plan_timezone=plan_timezone,
            abandoned=abandoned,
        )


async def start_task(
    session: AsyncSession,
    *,
    task_row: dict[str, Any],
    user_id: UUID,
    expected_version: int,
    now: datetime,
) -> dict[str, Any]:
    """start（§12.3）：pending → in_progress + 创建 Session。"""
    new_status = task_state.apply_transition(str(task_row["status"]), "start")
    if not await repo.update_task_status_cas(
        session,
        task_id=UUID(str(task_row["task_id"])),
        expected_version=expected_version,
        new_status=new_status,
        started_at=now,
    ):
        await _task_version_conflict(session, task_id=UUID(str(task_row["task_id"])))
    await repo.insert_task_event(
        session,
        event_id=uuid4(),
        task_id=UUID(str(task_row["task_id"])),
        event_type="started",
    )
    from backend.study.services.session_service import create_or_reuse_session

    await create_or_reuse_session(session, user_id=user_id, task_row=task_row, launch=False)
    await session.commit()
    return await get_task(session, user_id=user_id, task_id=UUID(str(task_row["task_id"])))


async def launch_task(
    session: AsyncSession,
    *,
    task_row: dict[str, Any],
    user_id: UUID,
    expected_version: int,
    now: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """launch（§12.3/§12.5/D24）：start + Session + 稳定响应骨架。"""
    new_status = task_state.launch_transition(str(task_row["status"]))
    if new_status != task_row["status"]:
        if not await repo.update_task_status_cas(
            session,
            task_id=UUID(str(task_row["task_id"])),
            expected_version=expected_version,
            new_status=new_status,
            started_at=now,
        ):
            await _task_version_conflict(session, task_id=UUID(str(task_row["task_id"])))
        await repo.insert_task_event(
            session,
            event_id=uuid4(),
            task_id=UUID(str(task_row["task_id"])),
            event_type="started",
        )
    from backend.study.services.session_service import create_or_reuse_session

    session_row, _created = await create_or_reuse_session(
        session, user_id=user_id, task_row=task_row, launch=True
    )
    refreshed_task = await get_task(
        session, user_id=user_id, task_id=UUID(str(task_row["task_id"]))
    )
    await session.commit()
    launch_payload = {
        "task_id": str(refreshed_task["task_id"]),
        "topic_key": refreshed_task["topic_key"],
        "graph_node_id": refreshed_task["graph_node_id"],
    }
    return refreshed_task, {
        "task_id": str(refreshed_task["task_id"]),
        "session_id": str(session_row["session_id"]),
        "conversation_thread_id": session_row["conversation_thread_id"],
        "conversation_status": session_row["conversation_status"],
        "launch_payload": launch_payload,
    }


async def complete_task(
    session: AsyncSession,
    *,
    task_row: dict[str, Any],
    expected_version: int,
    now: datetime,
    plan_timezone: str,
    idle_timeout: int,
) -> dict[str, Any]:
    """complete（§12.3/D27）：结算 Session → completed + completion_source=manual。"""
    new_status = task_state.apply_transition(str(task_row["status"]), "complete")
    await _settle_active_session(
        session,
        task_id=UUID(str(task_row["task_id"])),
        now=now,
        idle_timeout=idle_timeout,
        plan_timezone=plan_timezone,
        abandoned=False,
    )
    if not await repo.update_task_status_cas(
        session,
        task_id=UUID(str(task_row["task_id"])),
        expected_version=expected_version,
        new_status=new_status,
        completed_at=now,
        completion_source="manual",
    ):
        await _task_version_conflict(session, task_id=UUID(str(task_row["task_id"])))
    await repo.insert_task_event(
        session,
        event_id=uuid4(),
        task_id=UUID(str(task_row["task_id"])),
        event_type="completed",
        payload={"completion_source": "manual"},
    )
    local_date = now.astimezone(ZoneInfo(plan_timezone)).date()
    await repo.increment_daily_completed(
        session,
        user_id=await _task_user_id(session, task_id=UUID(str(task_row["task_id"]))),
        local_date=local_date,
    )
    await session.commit()
    return await repo.get_task_row(
        session,
        user_id=await _task_user_id(session, task_id=UUID(str(task_row["task_id"]))),
        task_id=UUID(str(task_row["task_id"])),
    )  # type: ignore[return-value]


async def skip_task(
    session: AsyncSession,
    *,
    task_row: dict[str, Any],
    expected_version: int,
    now: datetime,
    plan_timezone: str,
    idle_timeout: int,
) -> dict[str, Any]:
    """skip（§12.3）：in_progress 时活跃 Session 结算为 abandoned。"""
    new_status = task_state.apply_transition(str(task_row["status"]), "skip")
    if task_row["status"] == "in_progress":
        await _settle_active_session(
            session,
            task_id=UUID(str(task_row["task_id"])),
            now=now,
            idle_timeout=idle_timeout,
            plan_timezone=plan_timezone,
            abandoned=True,
        )
    if not await repo.update_task_status_cas(
        session,
        task_id=UUID(str(task_row["task_id"])),
        expected_version=expected_version,
        new_status=new_status,
    ):
        await _task_version_conflict(session, task_id=UUID(str(task_row["task_id"])))
    await repo.insert_task_event(
        session,
        event_id=uuid4(),
        task_id=UUID(str(task_row["task_id"])),
        event_type="skipped",
    )
    await session.commit()
    user_id = await _task_user_id(session, task_id=UUID(str(task_row["task_id"])))
    return await repo.get_task_row(session, user_id=user_id, task_id=UUID(str(task_row["task_id"])))  # type: ignore[return-value]


async def reopen_task(
    session: AsyncSession,
    *,
    task_row: dict[str, Any],
    expected_version: int,
) -> dict[str, Any]:
    """reopen（§12.3/D11）：completed/skipped → pending，保留历史事件。"""
    new_status = task_state.apply_transition(str(task_row["status"]), "reopen")
    if not await repo.update_task_status_cas(
        session,
        task_id=UUID(str(task_row["task_id"])),
        expected_version=expected_version,
        new_status=new_status,
        completed_at=None,
        completion_source=None,
    ):
        await _task_version_conflict(session, task_id=UUID(str(task_row["task_id"])))
    await repo.insert_task_event(
        session,
        event_id=uuid4(),
        task_id=UUID(str(task_row["task_id"])),
        event_type="reopened",
    )
    await session.commit()
    user_id = await _task_user_id(session, task_id=UUID(str(task_row["task_id"])))
    return await repo.get_task_row(session, user_id=user_id, task_id=UUID(str(task_row["task_id"])))  # type: ignore[return-value]


async def reschedule_task(
    session: AsyncSession,
    *,
    task_row: dict[str, Any],
    plan_row: dict[str, Any],
    expected_version: int,
    scheduled_date: date,
    today_local: date,
) -> dict[str, Any]:
    """reschedule（§12.3/D11）：仅 pending；只改自身日期 + 碰撞检测。"""
    if task_row["status"] != "pending":
        raise StudyInvalidTaskTransitionError(
            f"任务状态 {task_row['status']} 不允许 reschedule（仅 pending）"
        )
    if scheduled_date < today_local or scheduled_date > plan_row["target_date"]:
        raise StudyScheduleConflictError("目标日期必须在今天与计划截止日期之间")
    availability = await repo.list_availability(session, plan_id=UUID(str(plan_row["plan_id"])))
    by_dow = {int(a["day_of_week"]): a for a in availability}
    slot = by_dow.get(scheduled_date.isoweekday())
    if slot is None or bool(slot["is_rest_day"]):
        raise StudyScheduleConflictError("目标日期是休息日，不能安排任务")
    planned, _completed = await repo.sum_tasks_minutes_for_date(
        session,
        plan_id=UUID(str(plan_row["plan_id"])),
        scheduled_date=scheduled_date,
        exclude_task_id=UUID(str(task_row["task_id"])),
    )
    if planned + int(task_row["estimated_minutes"]) > int(slot["available_minutes"]):
        raise StudyScheduleConflictError("目标日已有任务分钟与当前任务之和超过当天可用分钟")
    if not await repo.update_task_scheduled_date_cas(
        session,
        task_id=UUID(str(task_row["task_id"])),
        expected_version=expected_version,
        scheduled_date=scheduled_date,
    ):
        await _task_version_conflict(session, task_id=UUID(str(task_row["task_id"])))
    await repo.insert_task_event(
        session,
        event_id=uuid4(),
        task_id=UUID(str(task_row["task_id"])),
        event_type="rescheduled",
        payload={"scheduled_date": scheduled_date.isoformat()},
    )
    await session.commit()
    user_id = await _task_user_id(session, task_id=UUID(str(task_row["task_id"])))
    return await repo.get_task_row(session, user_id=user_id, task_id=UUID(str(task_row["task_id"])))  # type: ignore[return-value]


async def _task_user_id(session: AsyncSession, *, task_id: UUID) -> UUID:
    result = await session.execute(
        __import__("sqlalchemy", fromlist=["text"]).text(
            "SELECT p.user_id FROM study_tasks t JOIN study_plans p ON p.plan_id = t.plan_id "
            "WHERE t.task_id = :task_id"
        ),
        {"task_id": task_id},
    )
    value = result.scalar_one_or_none()
    if value is None:
        raise StudyTaskNotFoundError("任务不存在")
    return UUID(str(value))


async def _task_version_conflict(session: AsyncSession, *, task_id: UUID) -> None:
    current = await repo.get_task_version(session, task_id=task_id)
    if current is None:
        raise StudyTaskNotFoundError("任务不存在或不属于当前用户")
    raise StudyTaskVersionConflictError("任务版本冲突，请刷新后重试", current_version=current)
