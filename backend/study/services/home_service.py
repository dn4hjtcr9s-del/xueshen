"""Study 首页聚合服务（§12.6/D9/D29/§13，GET 无副作用）。

- active plan 摘要 + 双口径进度 + personalization_status（D29）；
- 今日正式任务（Phase 3 起与 daily feed 合并，本期 recommendations 恒为空）；
- 近 7 天真实活跃分钟（§13.3：七个自然日补零）；
- generation_status：Phase 1 无 feed run → pending；无 active plan → no_active_plan。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from backend.study.persistence import repositories as repo
from backend.study.services.progress import dual_progress


async def aggregate_home(
    session: AsyncSession,
    *,
    user_id: UUID,
    local_date: date,
) -> dict[str, Any]:
    """§12.6：首页聚合（只读，无副作用）。"""
    plan = await repo.get_active_plan_row(session, user_id=user_id)
    if plan is None:
        return {
            "local_date": local_date,
            "timezone": None,
            "active_plan": None,
            "today": {
                "generation_status": "no_active_plan",
                "completed_count": 0,
                "total_count": 0,
                "planned_minutes": 0,
                "tasks": [],
                "recommendations": [],
            },
            "recent_7_days": {
                "from": local_date - timedelta(days=6),
                "to": local_date,
                "total_active_minutes": 0,
                "days": [
                    {
                        "local_date": d,
                        "active_minutes": 0,
                        "completed_task_count": 0,
                        "session_count": 0,
                    }
                    for d in _seven_days(local_date)
                ],
            },
        }

    plan_id = UUID(str(plan["plan_id"]))
    revision_id = plan["current_revision_id"]
    revision_tasks = (
        await repo.list_revision_tasks(session, revision_id=UUID(str(revision_id)))
        if revision_id is not None
        else []
    )
    non_cancelled = [t for t in revision_tasks if t["status"] != "cancelled"]
    completed = [t for t in non_cancelled if t["status"] == "completed"]
    task_progress, workload_progress = dual_progress(
        completed_count=len(completed),
        total_count=len(non_cancelled),
        completed_minutes=sum(int(t["estimated_minutes"]) for t in completed),
        total_minutes=sum(int(t["estimated_minutes"]) for t in non_cancelled),
    )

    today_tasks = await repo.list_tasks_for_date(
        session, plan_id=plan_id, scheduled_date=local_date
    )
    today_completed = sum(1 for t in today_tasks if t["status"] == "completed")
    week_label = _week_label(plan["start_date"], local_date)

    recent = await repo.get_daily_stats_between(
        session,
        user_id=user_id,
        from_date=local_date - timedelta(days=6),
        to_date=local_date,
    )
    recent_map = {r["local_date"]: r for r in recent}
    days = []
    total_minutes = 0
    for d in _seven_days(local_date):
        row = recent_map.get(d)
        minutes = int(row["active_seconds"]) // 60 if row else 0
        total_minutes += minutes
        days.append(
            {
                "local_date": d,
                "active_minutes": minutes,
                "completed_task_count": int(row["completed_task_count"]) if row else 0,
                "session_count": int(row["session_count"]) if row else 0,
            }
        )

    return {
        "local_date": local_date,
        "timezone": plan["timezone"],
        "active_plan": {
            "plan_id": str(plan_id),
            "goal": plan["goal"],
            "week_label": week_label,
            "personalization_status": plan.get("personalization_status"),
            "progress_percent": task_progress,
            "task_progress_percent": task_progress,
            "workload_progress_percent": workload_progress,
        },
        "today": {
            "generation_status": "pending",
            "completed_count": today_completed,
            "total_count": len(today_tasks),
            "planned_minutes": sum(int(t["estimated_minutes"]) for t in today_tasks),
            "tasks": today_tasks,
            "recommendations": [],
        },
        "recent_7_days": {
            "from": local_date - timedelta(days=6),
            "to": local_date,
            "total_active_minutes": total_minutes,
            "days": days,
        },
    }


def _seven_days(end: date) -> list[date]:
    return [end - timedelta(days=6 - i) for i in range(7)]


def _week_label(start_date: date, local_date: date) -> str:
    """第 N 周（§12.6 示例：按计划开始日计算）。"""
    week = max(1, (local_date - start_date).days // 7 + 1)
    return f"第 {week} 周"


def server_today(timezone: str, now: datetime | None = None) -> date:
    """服务端判定的"今天"（§12.6：不信任浏览器计算）。"""
    return (now or datetime.now().astimezone()).astimezone(ZoneInfo(timezone)).date()
