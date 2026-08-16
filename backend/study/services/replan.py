"""Study Replan 服务（§9.4/§10.12/D18/D21，v1.2，Phase 4）。

- 自动调整只允许修改未来未完成且未锁定的任务（§9.4）；已完成/锁定不动（§10.10/10.11）；
- 未完成任务顺延：把 overdue pending 任务移动到最早可容纳的未来非休息日；
  放不下 → 重大调整（须人工确认，§10.13 不无限堆积）；
- D18 量化阈值全部写成纯函数并覆盖边界测试，禁止自然语言判断；
- 重大调整生成 proposed revision（operation → needs_input），局部调整
  （study_auto_replan_enabled 或用户显式调整）自动激活。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.study.contracts.domain import MAX_TASKS_PER_DAY
from backend.study.persistence import repositories as repo

# ---------------------------------------------------------------------------
# D18 量化阈值（纯函数，§20.1 覆盖边界）
# ---------------------------------------------------------------------------


def classify_adjustment(
    *,
    base_daily_minutes: dict[date, int],
    new_daily_minutes: dict[date, int],
    session_min_minutes: int,
    removed_incomplete_ratio: float,
    base_target_date: date,
    new_target_date: date,
    scope_changed: bool,
    core_chapters_changed: bool,
) -> tuple[bool, bool, list[str]]:
    """返回 (major, high_impact, reasons)。

    - 任一未来学习日负载增加 ≥30% 且至少 +15 分钟（D18）；
    - 移除未完成任务 ≥20%（D18）；
    - 目标日期平移 ≥7 天 → high_impact；任何目标日期变化 → major（D18）；
    - 计划范围/核心章节变化 → major（D18）。
    """
    reasons: list[str] = []
    major = False
    high_impact = False
    for day, new_minutes in new_daily_minutes.items():
        old_minutes = base_daily_minutes.get(day, 0)
        delta = new_minutes - old_minutes
        if delta >= 15 and (delta / max(old_minutes, session_min_minutes)) >= 0.30:
            major = True
            reasons.append(f"负载增加:{day.isoformat()}:+{delta}min")
    if removed_incomplete_ratio >= 0.20:
        major = True
        reasons.append("移除未完成任务≥20%")
    if new_target_date != base_target_date:
        major = True
        if abs((new_target_date - base_target_date).days) >= 7:
            high_impact = True
            reasons.append("目标日期变化≥7天")
        else:
            reasons.append("目标日期变化")
    if scope_changed:
        major = True
        reasons.append("计划范围变化")
    if core_chapters_changed:
        major = True
        reasons.append("核心章节变化")
    return major, high_impact, reasons


def daily_minutes_from_tasks(tasks: list[dict[str, Any]]) -> dict[date, int]:
    """任务列表 → 每日负载（非取消任务分钟）。"""
    result: dict[date, int] = {}
    for task in tasks:
        if task["status"] == "cancelled":
            continue
        d = task["scheduled_date"]
        result[d] = result.get(d, 0) + int(task["estimated_minutes"])
    return result


# ---------------------------------------------------------------------------
# Replan 构建与 diff 应用
# ---------------------------------------------------------------------------


async def build_carryover_replan(
    session: AsyncSession,
    *,
    plan: dict[str, Any],
    reason: str,
    today: date,
    proposal_operation_id: UUID | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool, bool]:
    """按当前 active revision 构建顺延 revision（§9.4）。

    返回 (revision_row, task_diff, major, high_impact)。
    task_diff 的 op：keep（复制，可含新日期）/ remove（移除）。
    """
    # 计划行锁：同一计划的 replan/决策串行，next_revision_no 无竞态（必改 #4）
    await session.execute(
        text("SELECT plan_id FROM study_plans WHERE plan_id = :plan_id FOR UPDATE"),
        {"plan_id": plan["plan_id"]},
    )
    base_revision_id = plan["current_revision_id"]
    if base_revision_id is None:
        raise ValueError("计划缺少 active revision")
    base_tasks = await repo.list_revision_tasks(session, revision_id=UUID(str(base_revision_id)))
    availability = await repo.list_availability(session, plan_id=UUID(str(plan["plan_id"])))
    by_dow = {int(a["day_of_week"]): int(a["available_minutes"]) for a in availability}
    target_date = plan["target_date"]

    # 未来非休息日桶（D18 判定用每日负载）
    future_days: list[date] = []
    cursor = today
    while cursor <= target_date:
        if by_dow.get(cursor.isoweekday(), 0) > 0:
            future_days.append(cursor)
        cursor += timedelta(days=1)

    diff: list[dict[str, Any]] = []
    # 容量负载只算未完成（completed 已完成，不占剩余容量；评审必改 #8a）
    day_load: dict[date, int] = {}
    day_count: dict[date, int] = {}
    for task in base_tasks:
        if task["status"] == "cancelled":
            diff.append({"op": "remove", "task_id": str(task["task_id"])})
            continue
        if task["status"] == "completed":
            # 已完成任务保留并计入进度（§10.11 不得修改），但不占剩余容量
            diff.append(
                {
                    "op": "keep",
                    "task_id": str(task["task_id"]),
                    "scheduled_date": task["scheduled_date"].isoformat(),
                }
            )
            continue
        d = task["scheduled_date"]
        day_load[d] = day_load.get(d, 0) + int(task["estimated_minutes"])
        day_count[d] = day_count.get(d, 0) + 1
    base_daily = dict(day_load)

    moves: list[dict[str, Any]] = []
    impossible = False
    for task in base_tasks:
        if task["status"] != "pending" or bool(task["user_locked"]):
            diff.append(
                {
                    "op": "keep",
                    "task_id": str(task["task_id"]),
                    "scheduled_date": task["scheduled_date"].isoformat(),
                }
            )
            continue
        if task["scheduled_date"] >= today:
            diff.append(
                {
                    "op": "keep",
                    "task_id": str(task["task_id"]),
                    "scheduled_date": task["scheduled_date"].isoformat(),
                }
            )
            continue
        # overdue pending → 顺延到最早可容纳的未来日
        minutes = int(task["estimated_minutes"])
        placed: date | None = None
        for day in future_days:
            cap = by_dow.get(day.isoweekday(), 0)
            if day_load.get(day, 0) + minutes <= cap and day_count.get(day, 0) < MAX_TASKS_PER_DAY:
                placed = day
                day_load[day] = day_load.get(day, 0) + minutes
                day_count[day] = day_count.get(day, 0) + 1
                break
        if placed is None:
            impossible = True
            placed = task["scheduled_date"]
        else:
            # 从旧日负载中扣除，避免同一任务双计（评审必改 #8b）
            old_date = task["scheduled_date"]
            day_load[old_date] = max(0, day_load.get(old_date, 0) - minutes)
        moves.append(
            {
                "op": "keep",
                "task_id": str(task["task_id"]),
                "scheduled_date": placed.isoformat(),
                "moved_from": task["scheduled_date"].isoformat(),
            }
        )
        diff.append(moves[-1])

    options: list[str] = []
    major = impossible
    high_impact = False
    if impossible:
        # §10.12/§10.13：不静默改目标日期；在 revision 快照中提出可选方案
        total_remaining = sum(
            int(t["estimated_minutes"])
            for t in base_tasks
            if t["status"] not in ("completed", "cancelled")
        )
        weekly_minutes = int(plan["weekly_minutes"])
        extra_weeks = max(1, -(-total_remaining // max(weekly_minutes, 1)))
        options = [
            f"延长截止日期约 {extra_weeks} 周",
            "增加每周可用时间",
            "缩小学习范围（移除部分未完成任务）",
        ]
        major = True
        high_impact = extra_weeks >= 1
    else:
        major, high_impact, _reasons = classify_adjustment(
            base_daily_minutes=base_daily,
            new_daily_minutes=day_load,
            session_min_minutes=int(plan["session_min_minutes"]),
            removed_incomplete_ratio=0.0,
            base_target_date=target_date,
            new_target_date=target_date,
            scope_changed=False,
            core_chapters_changed=False,
        )

    revision = await _insert_replan_revision(
        session,
        plan=plan,
        reason=reason,
        diff=diff,
        options=options,
        proposal_operation_id=proposal_operation_id,
    )
    return revision, diff, major, high_impact


async def apply_revision_diff(
    session: AsyncSession,
    *,
    plan: dict[str, Any],
    revision_row: dict[str, Any],
) -> None:
    """accept 时应用任务 diff（§12.2/D21）：新 revision 生成自己的任务行。

    已完成任务保留 status=completed；其余 keep 任务复制为 pending（历史
    由 base revision 行保留）；remove 任务不复制。审计事件 created。
    """
    diff = (revision_row["input_snapshot"] or {}).get("task_diff", [])
    kept_ids = {d["task_id"] for d in diff if d["op"] == "keep"}
    base_revision_id = revision_row["base_revision_id"]
    base_tasks = (
        await repo.list_revision_tasks(session, revision_id=UUID(str(base_revision_id)))
        if base_revision_id
        else []
    )
    new_revision_id = UUID(str(revision_row["revision_id"]))
    order_by_date: dict[date, int] = {}
    for task in base_tasks:
        tid = str(task["task_id"])
        if tid not in kept_ids or task["status"] == "cancelled":
            continue
        move = next((d for d in diff if d["task_id"] == tid), None)
        new_date = date.fromisoformat(move["scheduled_date"]) if move else task["scheduled_date"]
        order_by_date[new_date] = order_by_date.get(new_date, 0) + 1
        new_task_id = uuid4()
        await repo.insert_task(
            session,
            task_id=new_task_id,
            plan_id=UUID(str(plan["plan_id"])),
            revision_id=new_revision_id,
            scheduled_date=new_date,
            order_index=order_by_date[new_date],
            task_type=str(task["task_type"]),
            title=str(task["title"]),
            description=str(task["description"]),
            estimated_minutes=int(task["estimated_minutes"]),
            model_estimated_minutes=task["model_estimated_minutes"],
            estimation_basis=str(task["estimation_basis"]),
            topic_key=task["topic_key"],
            graph_node_id=task["graph_node_id"],
            source=str(task["source"]),
            source_feed_item_id=task["source_feed_item_id"],
            reason_codes=task["reason_codes"] or [],
        )
        await repo.insert_task_event(
            session,
            event_id=uuid4(),
            task_id=new_task_id,
            event_type="created",
            payload={"replan": True, "from_task_id": tid},
        )
        if task["status"] == "completed":
            await session.execute(
                text(
                    "UPDATE study_tasks SET status = 'completed', completion_source = 'manual', "
                    "completed_at = :completed_at WHERE task_id = :task_id"
                ),
                {"completed_at": task["completed_at"], "task_id": new_task_id},
            )


async def _insert_replan_revision(
    session: AsyncSession,
    *,
    plan: dict[str, Any],
    reason: str,
    diff: list[dict[str, Any]],
    options: list[str] | None = None,
    proposal_operation_id: UUID | None = None,
) -> dict[str, Any]:
    revision_id = uuid4()
    revision_no = await repo.next_revision_no(session, plan_id=UUID(str(plan["plan_id"])))
    await repo.insert_revision(
        session,
        revision_id=revision_id,
        plan_id=UUID(str(plan["plan_id"])),
        revision_no=revision_no,
        reason=reason,
        input_snapshot={"task_diff": diff, "options": options or []},
        personalization_status="not_requested",
        personalization_reason=None,
        change_summary=(
            "顺延未完成任务；截止日期内放不下，需人工确认可选方案（§10.12）"
            if options
            else (f"自动调整（{reason}）：顺延未完成任务" if diff else "无变化调整")
        ),
        proposal_operation_id=proposal_operation_id,
        base_revision_id=plan["current_revision_id"],
    )
    await session.commit()
    row = await repo.get_revision_row(
        session, plan_id=UUID(str(plan["plan_id"])), revision_id=revision_id
    )
    assert row is not None
    return row


async def run_replan_operation(
    session: AsyncSession,
    *,
    operation: dict[str, Any],
    settings: Any,
) -> dict[str, Any]:
    """Worker 执行 weekly_replan/user_adjustment（Phase 4，无模型确定性路径）。

    重大调整 → operation needs_input（等 accept/reject）；局部且自动启用 →
    直接激活（apply diff + supersede base + operation succeeded）。
    """
    from backend.study.contracts.errors import StudyPlanNotFoundError

    payload = operation["payload"] or {}
    plan = await repo.get_plan_row(
        session, user_id=UUID(str(operation["user_id"])), plan_id=UUID(str(payload["plan_id"]))
    )
    if plan is None:
        raise StudyPlanNotFoundError("计划不存在或不属于当前用户")
    today = datetime.now().astimezone(ZoneInfo(str(plan["timezone"]))).date()
    revision, _diff, major, _high = await build_carryover_replan(
        session,
        plan=plan,
        reason=str(payload.get("reason", "weekly_replan")),
        today=today,
        proposal_operation_id=UUID(str(operation["operation_id"])),
    )
    revision_id = UUID(str(revision["revision_id"]))
    plan_id = UUID(str(plan["plan_id"]))
    if major:
        await repo.update_operation_status(
            session,
            operation_id=UUID(str(operation["operation_id"])),
            expected_status="running",
            new_status="needs_input",
            result_payload={"revision_id": str(revision_id)},
        )
        await session.commit()
        return {"revision_id": str(revision_id), "status": "needs_input"}
    # 局部调整自动激活（仅当 STUDY_AUTO_REPLAN_ENABLED 或用户显式发起）
    auto_allowed = bool(payload.get("user_requested")) or settings.study_auto_replan_enabled
    if not auto_allowed:
        await repo.update_operation_status(
            session,
            operation_id=UUID(str(operation["operation_id"])),
            expected_status="running",
            new_status="needs_input",
            result_payload={"revision_id": str(revision_id)},
        )
        await session.commit()
        return {"revision_id": str(revision_id), "status": "needs_input"}
    if plan["current_revision_id"] is not None:
        await repo.mark_revision_superseded(
            session, revision_id=UUID(str(plan["current_revision_id"]))
        )
    await repo.update_revision_status(
        session,
        revision_id=revision_id,
        expected_status="proposed",
        new_status="active",
        decision_at=datetime.now().astimezone(),
        decision_actor_id=UUID(str(operation["user_id"])),
        decision_reason="自动调整（局部）",
    )
    await repo.set_current_revision(session, plan_id=plan_id, revision_id=revision_id)
    await apply_revision_diff(session, plan=plan, revision_row=revision)
    await repo.update_operation_status(
        session,
        operation_id=UUID(str(operation["operation_id"])),
        expected_status="running",
        new_status="succeeded",
        result_payload={"revision_id": str(revision_id)},
    )
    await session.commit()
    return {"revision_id": str(revision_id), "status": "succeeded"}
