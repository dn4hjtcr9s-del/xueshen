"""Study 推荐处理服务（§11.2/§12.4/D13，v1.2）。

accept 创建正式任务（D13）：同一事务锁定 active plan/revision，新任务挂
接受时的 active revision_id、source=recommendation、source_feed_item_id；
过期/修订已变化/超当天预算 → 409，不能静默塞入超额任务。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.study.contracts.errors import (
    StudyFeedItemNotFoundError,
    StudyRecommendationExpiredError,
    StudyScheduleConflictError,
)
from backend.study.persistence import repositories as repo


async def _get_feed_item(
    session: AsyncSession, *, user_id: UUID, feed_item_id: UUID
) -> tuple[dict[str, Any], dict[str, Any]]:
    """返回 (item, run)；user 作用域经 feed run 归属校验（§18.2）。"""
    result = await session.execute(
        text(
            """
            SELECT i.*, r.user_id AS run_user_id, r.plan_id, r.revision_id,
                   r.local_date, r.timezone, r.status AS run_status
            FROM study_daily_feed_items i
            JOIN study_daily_feed_runs r ON r.feed_run_id = i.feed_run_id
            WHERE i.feed_item_id = :feed_item_id
            """
        ),
        {"feed_item_id": feed_item_id},
    )
    row = result.mappings().first()
    if row is None or UUID(str(row["run_user_id"])) != user_id:
        raise StudyFeedItemNotFoundError("推荐不存在或不属于当前用户")
    item = {k: v for k, v in dict(row).items() if k not in {"run_user_id", "run_status"}}
    run = {
        "plan_id": row["plan_id"],
        "revision_id": row["revision_id"],
        "local_date": row["local_date"],
        "timezone": row["timezone"],
        "status": row["run_status"],
    }
    return item, run


async def accept_recommendation(
    session: AsyncSession,
    *,
    user_id: UUID,
    feed_item_id: UUID,
    now: datetime,
) -> dict[str, Any]:
    """把推荐加入今日正式任务（§11.2/D13）。"""
    item, run = await _get_feed_item(session, user_id=user_id, feed_item_id=feed_item_id)
    if item["status"] != "active" or (item["expires_at"] and now >= item["expires_at"]):
        raise StudyRecommendationExpiredError("推荐已过期或已处理，请刷新首页")
    plan = await repo.get_active_plan_row(session, user_id=user_id)
    if plan is None or UUID(str(plan["plan_id"])) != UUID(str(run["plan_id"])):
        raise StudyRecommendationExpiredError("active plan 已变化，请刷新首页")
    if run["revision_id"] is not None and plan["current_revision_id"] != run["revision_id"]:
        raise StudyRecommendationExpiredError("active revision 已变化，请刷新首页")

    availability = await repo.list_availability(session, plan_id=UUID(str(plan["plan_id"])))
    by_dow = {int(a["day_of_week"]): a for a in availability}
    local_date = run["local_date"]
    slot = by_dow.get(local_date.isoweekday())
    planned, _completed = await repo.sum_tasks_minutes_for_date(
        session, plan_id=UUID(str(plan["plan_id"])), scheduled_date=local_date
    )
    if slot is None or bool(slot["is_rest_day"]):
        raise StudyScheduleConflictError("今天是休息日，不能加入正式任务")
    if planned + int(item["estimated_minutes"] or 0) > int(slot["available_minutes"]):
        raise StudyScheduleConflictError("加入后超过当天可用分钟，请改走 plan adjustment")

    task_id = uuid4()
    await repo.insert_task(
        session,
        task_id=task_id,
        plan_id=UUID(str(plan["plan_id"])),
        revision_id=UUID(str(run["revision_id"] or plan["current_revision_id"])),
        scheduled_date=local_date,
        order_index=999,
        task_type="learn",
        title=str(item["title"]),
        description=str(item["reason"]),
        estimated_minutes=int(item["estimated_minutes"] or plan["session_min_minutes"]),
        model_estimated_minutes=None,
        estimation_basis="original",
        topic_key=item["topic_key"],
        graph_node_id=item["graph_node_id"],
        source="recommendation",
        source_feed_item_id=feed_item_id,
        reason_codes=item["reason_codes"] or [],
    )
    await repo.insert_task_event(
        session,
        event_id=uuid4(),
        task_id=task_id,
        event_type="created",
        payload={"source": "recommendation", "feed_item_id": str(feed_item_id)},
    )
    await session.execute(
        text(
            "UPDATE study_daily_feed_items SET status = 'accepted' "
            "WHERE feed_item_id = :feed_item_id"
        ),
        {"feed_item_id": feed_item_id},
    )
    await session.commit()
    return {"feed_item_id": str(feed_item_id), "task_id": str(task_id)}


async def dismiss_recommendation(
    session: AsyncSession, *, user_id: UUID, feed_item_id: UUID
) -> dict[str, Any]:
    item, _run = await _get_feed_item(session, user_id=user_id, feed_item_id=feed_item_id)
    if item["status"] not in ("active", "accepted"):
        raise StudyRecommendationExpiredError("推荐已处理")
    await session.execute(
        text(
            "UPDATE study_daily_feed_items SET status = 'dismissed' "
            "WHERE feed_item_id = :feed_item_id"
        ),
        {"feed_item_id": feed_item_id},
    )
    await session.commit()
    return {"feed_item_id": str(feed_item_id), "status": "dismissed"}
