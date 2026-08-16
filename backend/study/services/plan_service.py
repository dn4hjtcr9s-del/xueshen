"""Study 计划领域服务（方案 §12.2/D8/D11/D13/D21/D25/D26）。

- manual 直录：PlanIntent + task_blueprint → 确定性排期 → draft plan/revision/tasks；
- ai 路径：只创建 queued operation（Phase 2 worker 处理）；
- activate/pause/resume/archive 生命周期：CAS + active 唯一约束 + 审计事件；
- calendar/revisions/proposed 决策：纯状态转移（任务 diff 应用在 Phase 4 replan）。

所有写入在同一 Study DB 事务内提交（§15.4）；并发由 expected_version + 唯一索引兜底。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from backend.study.contracts.api import PlanCreateRequest, PlanIntent
from backend.study.contracts.errors import (
    ActiveStudyPlanExistsError,
    StudyInvalidPlanTransitionError,
    StudyInvalidRevisionTransitionError,
    StudyPlanNotFoundError,
    StudyRevisionNotFoundError,
)
from backend.study.persistence import repositories as repo
from backend.study.services import scheduling

#: ai 生成 operation 类型（Phase 2 worker 消费）
OP_PLAN_GENERATION = "plan_generation"


async def create_plan(
    session: AsyncSession,
    *,
    user_id: UUID,
    request: PlanCreateRequest,
    settings: Any,
) -> dict[str, Any]:
    """创建计划（§12.2）。manual 同步出草案；ai 只建 operation 返回 202。"""
    if request.generation_mode == "ai":
        operation_id = uuid4()
        await repo.insert_operation(
            session,
            operation_id=operation_id,
            user_id=user_id,
            operation_type=OP_PLAN_GENERATION,
            payload={"intent": request.intent.model_dump(mode="json")},
        )
        await session.commit()
        return {"mode": "ai", "operation_id": operation_id, "plan": None}
    return {"mode": "manual", "plan": await create_manual_plan(session, user_id, request)}


async def create_manual_plan(
    session: AsyncSession, user_id: UUID, request: PlanCreateRequest
) -> dict[str, Any]:
    """manual 直录（§8/D8）：确定性排期同步生成 draft plan/revision/tasks。"""
    intent = request.intent
    return await persist_plan_from_blueprint(
        session,
        user_id=user_id,
        intent=intent,
        blueprints=[
            (b.title, str(b.task_type), b.estimated_minutes, b.topic_key, b.description)
            for b in request.task_blueprint
        ],
        generation_mode="manual",
        personalization_status="not_requested",
        personalization_reason=None,
        change_summary="初始计划草案（manual 直录，确定性排期）",
        memory_context_hash=None,
        model_name=None,
        prompt_version=None,
    )


async def persist_plan_from_blueprint(
    session: AsyncSession,
    *,
    user_id: UUID,
    intent: PlanIntent,
    blueprints: list[tuple[str, str, int, str | None, str]],
    generation_mode: str,
    personalization_status: str,
    personalization_reason: str | None,
    change_summary: str,
    memory_context_hash: str | None,
    model_name: str | None,
    prompt_version: str | None,
) -> dict[str, Any]:
    """按蓝图确定性排期并落库 draft plan/revision/tasks（§9.2 复用 Phase 1 引擎）。"""
    plan_id = uuid4()
    revision_id = uuid4()
    target_date = intent.resolved_target_date()

    await repo.insert_plan(
        session,
        plan_id=plan_id,
        user_id=user_id,
        goal=intent.goal,
        timezone=intent.timezone,
        start_date=intent.start_date,
        target_date=target_date,
        weekly_minutes=intent.weekly_total_minutes(),
        session_min_minutes=intent.session_min_minutes,
        session_max_minutes=intent.session_max_minutes,
    )
    for slot in intent.weekly_availability:
        await repo.insert_availability(
            session,
            plan_id=plan_id,
            day_of_week=slot.day_of_week,
            available_minutes=slot.available_minutes,
            start_local_time=slot.start_local_time.isoformat() if slot.start_local_time else None,
            end_local_time=slot.end_local_time.isoformat() if slot.end_local_time else None,
            is_rest_day=slot.is_rest_day,
        )

    availability_map = {
        slot.day_of_week: scheduling.DayPlan(
            local_date=date.min,
            day_of_week=slot.day_of_week,
            available_minutes=slot.available_minutes,
            is_rest_day=slot.is_rest_day,
        )
        for slot in intent.weekly_availability
    }
    days = scheduling.plan_days(intent.start_date, target_date, availability_map)
    scheduled = scheduling.schedule_manual_blueprint(
        days=days,
        session_min=intent.session_min_minutes,
        session_max=intent.session_max_minutes,
        blueprints=blueprints,
    )

    await repo.insert_revision(
        session,
        revision_id=revision_id,
        plan_id=plan_id,
        revision_no=1,
        reason="initial",
        input_snapshot={
            "intent": intent.model_dump(mode="json"),
            "generation_mode": generation_mode,
            "blueprint": [list(b) for b in blueprints],
        },
        personalization_status=personalization_status,
        personalization_reason=personalization_reason,
        change_summary=change_summary,
        proposal_operation_id=None,
        base_revision_id=None,
        memory_context_hash=memory_context_hash,
        model_name=model_name,
        prompt_version=prompt_version,
    )
    for draft in scheduled:
        task_id = uuid4()
        await repo.insert_task(
            session,
            task_id=task_id,
            plan_id=plan_id,
            revision_id=revision_id,
            scheduled_date=draft.scheduled_date,
            order_index=draft.order_index,
            task_type=draft.task_type,
            title=draft.title,
            description=draft.description,
            estimated_minutes=draft.estimated_minutes,
            model_estimated_minutes=draft.model_estimated_minutes,
            estimation_basis=draft.estimation_basis,
            topic_key=draft.topic_key,
            source="plan" if generation_mode == "ai" else "manual",
        )
        await repo.insert_task_event(
            session, event_id=uuid4(), task_id=task_id, event_type="created"
        )
    await repo.set_current_revision(session, plan_id=plan_id, revision_id=revision_id)
    await session.commit()
    plan = await repo.get_plan_row(session, user_id=user_id, plan_id=plan_id)
    assert plan is not None
    return plan


async def activate_plan(
    session: AsyncSession,
    *,
    user_id: UUID,
    plan_id: UUID,
    expected_version: int,
) -> dict[str, Any]:
    """激活草案（§12.2：draft → active；CAS + active 唯一约束兜底）。"""
    plan = await _require_plan(session, user_id, plan_id)
    if plan["status"] != "draft":
        raise StudyInvalidPlanTransitionError(f"只有 draft 计划可以激活，当前 {plan['status']}")
    revision = await _current_revision_row(session, plan)
    if revision is None:
        raise StudyInvalidPlanTransitionError("计划缺少可激活的 revision")
    now = datetime.now().astimezone()
    try:
        await repo.activate_plan_transactional(
            session,
            plan_id=plan_id,
            user_id=user_id,
            expected_version=expected_version,
            revision_id=UUID(str(revision["revision_id"])),
            now=now,
        )
    except Exception as exc:
        from sqlalchemy.exc import IntegrityError

        if isinstance(exc, IntegrityError) and repo.is_active_conflict(exc):
            raise ActiveStudyPlanExistsError("已存在其他 active 计划（D5）") from exc
        raise
    await session.commit()
    refreshed = await repo.get_plan_row(session, user_id=user_id, plan_id=plan_id)
    assert refreshed is not None
    return refreshed


async def pause_plan(
    session: AsyncSession,
    *,
    user_id: UUID,
    plan_id: UUID,
    expected_version: int,
) -> dict[str, Any]:
    plan = await _require_plan(session, user_id, plan_id)
    if plan["status"] != "active":
        raise StudyInvalidPlanTransitionError(f"只有 active 计划可以暂停，当前 {plan['status']}")
    if not await repo.update_plan_status(
        session, plan_id=plan_id, expected_version=expected_version, new_status="paused"
    ):
        await _raise_version_conflict(session, plan_id)
    await session.commit()
    return await _require_plan(session, user_id, plan_id)


async def resume_plan(
    session: AsyncSession,
    *,
    user_id: UUID,
    plan_id: UUID,
    expected_version: int,
) -> dict[str, Any]:
    plan = await _require_plan(session, user_id, plan_id)
    if plan["status"] != "paused":
        raise StudyInvalidPlanTransitionError(f"只有 paused 计划可以恢复，当前 {plan['status']}")
    try:
        await repo.resume_plan_transactional(
            session,
            plan_id=plan_id,
            user_id=user_id,
            expected_version=expected_version,
            now=datetime.now().astimezone(),
        )
    except Exception as exc:
        from sqlalchemy.exc import IntegrityError

        if isinstance(exc, IntegrityError) and repo.is_active_conflict(exc):
            raise ActiveStudyPlanExistsError("已存在其他 active 计划（D25）") from exc
        raise
    await session.commit()
    return await _require_plan(session, user_id, plan_id)


async def archive_plan(
    session: AsyncSession,
    *,
    user_id: UUID,
    plan_id: UUID,
    expected_version: int,
) -> dict[str, Any]:
    """归档（§12.2/D25）：非终态计划可归档；非终态任务 → cancelled + 事件。"""
    plan = await _require_plan(session, user_id, plan_id)
    if plan["status"] not in ("draft", "active", "paused", "completed"):
        raise StudyInvalidPlanTransitionError(f"当前状态 {plan['status']} 不允许归档（§12.2）")
    if not await repo.update_plan_status(
        session, plan_id=plan_id, expected_version=expected_version, new_status="archived"
    ):
        await _raise_version_conflict(session, plan_id)
    cancelled = await repo.cancel_tasks_for_plan_lifecycle(session, plan_id=plan_id)
    for task_id in cancelled:
        await repo.insert_task_event(
            session,
            event_id=uuid4(),
            task_id=task_id,
            event_type="cancelled",
            payload={"reason": "plan_archived"},
        )
    await session.commit()
    return await _require_plan(session, user_id, plan_id)


async def get_calendar(session: AsyncSession, *, user_id: UUID, plan_id: UUID) -> dict[str, Any]:
    """§12.2/D26：完整计划日期范围的周/日/任务结构（含休息日与空任务日）。"""
    plan = await _require_plan(session, user_id, plan_id)
    availability = await repo.list_availability(session, plan_id=plan_id)
    tasks = await repo.list_plan_tasks(session, plan_id=plan_id)
    availability_map = {int(a["day_of_week"]): a for a in availability}
    tasks_by_date: dict[date, list[dict[str, Any]]] = {}
    for t in tasks:
        tasks_by_date.setdefault(t["scheduled_date"], []).append(t)

    weeks: list[dict[str, Any]] = []
    current = plan["start_date"]
    week_index = 1
    day_rows: list[dict[str, Any]] = []
    while current <= plan["target_date"]:
        dow = current.isoweekday()
        a = availability_map.get(dow, {})
        day_tasks = sorted(tasks_by_date.get(current, []), key=lambda t: int(t["order_index"]))
        day_rows.append(
            {
                "local_date": current,
                "day_of_week": dow,
                "is_rest_day": bool(a.get("is_rest_day", True)),
                "available_minutes": int(a.get("available_minutes", 0)),
                "planned_minutes": sum(int(t["estimated_minutes"]) for t in day_tasks),
                "completed_minutes": sum(
                    int(t["estimated_minutes"]) for t in day_tasks if t["status"] == "completed"
                ),
                "tasks": day_tasks,
            }
        )
        if dow == 7 or current == plan["target_date"]:
            weeks.append(
                {
                    "week_index": week_index,
                    "from": day_rows[0]["local_date"],
                    "to": day_rows[-1]["local_date"],
                    "days": day_rows,
                }
            )
            week_index += 1
            day_rows = []
        current += timedelta(days=1)
    return {
        "plan_id": plan_id,
        "timezone": plan["timezone"],
        "start_date": plan["start_date"],
        "target_date": plan["target_date"],
        "current_revision_id": plan["current_revision_id"],
        "weeks": weeks,
    }


async def decide_revision(
    session: AsyncSession,
    *,
    user_id: UUID,
    plan_id: UUID,
    revision_id: UUID,
    expected_version: int,
    decision: str,
    reason: str | None,
) -> dict[str, Any]:
    """proposed revision 决策（§12.2/D21）：accept/reject + CAS + operation 终态。"""
    plan = await _require_plan(session, user_id, plan_id)
    revision = await repo.get_revision_row(session, plan_id=plan_id, revision_id=revision_id)
    if revision is None:
        raise StudyRevisionNotFoundError("revision 不存在或不属于当前计划")
    if revision["status"] != "proposed":
        raise StudyInvalidRevisionTransitionError(
            f"只有 proposed revision 可以决策，当前 {revision['status']}"
        )
    if (
        revision["base_revision_id"] is not None
        and revision["base_revision_id"] != plan["current_revision_id"]
    ):
        raise StudyInvalidRevisionTransitionError(
            "proposal 基于的 active revision 已变化，请重新生成调整（§12.2）"
        )
    if not await repo.bump_plan_version(
        session, plan_id=plan_id, expected_version=expected_version
    ):
        await _raise_version_conflict(session, plan_id)
    now = datetime.now().astimezone()
    if decision == "accept":
        if plan["current_revision_id"] is not None:
            await repo.mark_revision_superseded(
                session, revision_id=UUID(str(plan["current_revision_id"]))
            )
        updated = await repo.update_revision_status(
            session,
            revision_id=revision_id,
            expected_status="proposed",
            new_status="active",
            decision_at=now,
            decision_actor_id=user_id,
            decision_reason=reason,
        )
        if not updated:
            raise StudyInvalidRevisionTransitionError("revision 状态已变化，决策失败")
        await repo.set_current_revision(session, plan_id=plan_id, revision_id=revision_id)
        op_terminal = "succeeded"
    else:
        updated = await repo.update_revision_status(
            session,
            revision_id=revision_id,
            expected_status="proposed",
            new_status="rejected",
            decision_at=now,
            decision_actor_id=user_id,
            decision_reason=reason,
        )
        if not updated:
            raise StudyInvalidRevisionTransitionError("revision 状态已变化，决策失败")
        op_terminal = "cancelled"
    if revision["proposal_operation_id"] is not None:
        await repo.update_operation_status(
            session,
            operation_id=UUID(str(revision["proposal_operation_id"])),
            expected_status="needs_input",
            new_status=op_terminal,
            result_payload={"revision_id": str(revision_id), "decision": decision},
        )
    await session.commit()
    refreshed = await repo.get_revision_row(session, plan_id=plan_id, revision_id=revision_id)
    assert refreshed is not None
    return refreshed


async def _require_plan(session: AsyncSession, user_id: UUID, plan_id: UUID) -> dict[str, Any]:
    plan = await repo.get_plan_row(session, user_id=user_id, plan_id=plan_id)
    if plan is None:
        raise StudyPlanNotFoundError("计划不存在或不属于当前用户")
    return plan


async def _current_revision_row(
    session: AsyncSession, plan: dict[str, Any]
) -> dict[str, Any] | None:
    current = plan.get("current_revision_id")
    if current is None:
        return None
    return await repo.get_revision_row(
        session, plan_id=UUID(str(plan["plan_id"])), revision_id=UUID(str(current))
    )


async def _raise_version_conflict(session: AsyncSession, plan_id: UUID) -> None:
    from backend.study.contracts.errors import StudyPlanVersionConflictError

    current = await repo.get_plan_version(session, plan_id=plan_id)
    raise StudyPlanVersionConflictError("计划版本冲突，请刷新后重试", current_version=current)
