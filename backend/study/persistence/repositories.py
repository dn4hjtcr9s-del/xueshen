"""Study 持久化 Repository（方案 §7/§12，SQL 直写，与 Community 同风格）。

- 所有写入用 expected_version CAS（§15.3）；唯一约束冲突转域错误；
- 任务/事件/Session 变更同事务提交（§15.4），事件插入由 service 调用；
- 查询一律带 user_id 作用域（§18.2，不泄露他人数据）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.study.contracts.errors import (
    StudyPlanNotFoundError,
    StudyPlanVersionConflictError,
)


def _row(d: Any) -> dict[str, Any]:
    return dict(d)


def _json(value: Any) -> str:
    """jsonb 参数序列化（psycopg3 不直接适配 dict/list，与 Community 同模式）。"""
    import json

    return json.dumps(value, ensure_ascii=False)


def _rowcount(result: Any) -> int:
    """UPDATE/DELETE 影响行数（mypy 对 text() 结果类型宽松，统一取值）。"""
    return int(getattr(result, "rowcount", 0) or 0)


# ---------------------------------------------------------------------------
# 计划（§7.2）
# ---------------------------------------------------------------------------


async def insert_plan(
    session: AsyncSession,
    *,
    plan_id: UUID,
    user_id: UUID,
    goal: str,
    timezone: str,
    start_date: date,
    target_date: date,
    weekly_minutes: int,
    session_min_minutes: int,
    session_max_minutes: int,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO study_plans (plan_id, user_id, goal, timezone, start_date,
                target_date, weekly_minutes, session_min_minutes, session_max_minutes)
            VALUES (:plan_id, :user_id, :goal, :timezone, :start_date, :target_date,
                :weekly_minutes, :session_min_minutes, :session_max_minutes)
            """
        ),
        {
            "plan_id": plan_id,
            "user_id": user_id,
            "goal": goal,
            "timezone": timezone,
            "start_date": start_date,
            "target_date": target_date,
            "weekly_minutes": weekly_minutes,
            "session_min_minutes": session_min_minutes,
            "session_max_minutes": session_max_minutes,
        },
    )


async def get_plan_row(
    session: AsyncSession, *, user_id: UUID, plan_id: UUID
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            """
            SELECT p.*, r.personalization_status
            FROM study_plans p
            LEFT JOIN study_plan_revisions r ON r.revision_id = p.current_revision_id
            WHERE p.plan_id = :plan_id AND p.user_id = :user_id
            """
        ),
        {"plan_id": plan_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return _row(row) if row is not None else None


async def list_plan_rows(session: AsyncSession, *, user_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            """
            SELECT p.*, r.personalization_status
            FROM study_plans p
            LEFT JOIN study_plan_revisions r ON r.revision_id = p.current_revision_id
            WHERE p.user_id = :user_id
            ORDER BY p.created_at DESC
            """
        ),
        {"user_id": user_id},
    )
    return [_row(r) for r in result.mappings().all()]


async def get_active_plan_row(session: AsyncSession, *, user_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            """
            SELECT p.*, r.personalization_status
            FROM study_plans p
            LEFT JOIN study_plan_revisions r ON r.revision_id = p.current_revision_id
            WHERE p.user_id = :user_id AND p.status = 'active'
            """
        ),
        {"user_id": user_id},
    )
    row = result.mappings().first()
    return _row(row) if row is not None else None


async def update_plan_status(
    session: AsyncSession,
    *,
    plan_id: UUID,
    expected_version: int,
    new_status: str,
    activated_at: datetime | None = None,
    now: datetime | None = None,
) -> int:
    """CAS 更新计划状态；返回当前版本（冲突时用于错误提示）。"""
    result = await session.execute(
        text(
            """
            UPDATE study_plans
            SET status = :status, version = version + 1,
                activated_at = COALESCE(:activated_at, activated_at),
                updated_at = COALESCE(:now, now())
            WHERE plan_id = :plan_id AND version = :expected_version
            """
        ),
        {
            "status": new_status,
            "activated_at": activated_at,
            "now": now,
            "plan_id": plan_id,
            "expected_version": expected_version,
        },
    )
    return _rowcount(result)


async def bump_plan_version(session: AsyncSession, *, plan_id: UUID, expected_version: int) -> int:
    result = await session.execute(
        text(
            "UPDATE study_plans SET version = version + 1, updated_at = now() "
            "WHERE plan_id = :plan_id AND version = :expected_version"
        ),
        {"plan_id": plan_id, "expected_version": expected_version},
    )
    return _rowcount(result)


async def get_plan_version(session: AsyncSession, *, plan_id: UUID) -> int | None:
    result = await session.execute(
        text("SELECT version FROM study_plans WHERE plan_id = :plan_id"),
        {"plan_id": plan_id},
    )
    return result.scalar_one_or_none()


async def set_current_revision(
    session: AsyncSession, *, plan_id: UUID, revision_id: UUID | None
) -> None:
    await session.execute(
        text(
            "UPDATE study_plans SET current_revision_id = :revision_id, updated_at = now() "
            "WHERE plan_id = :plan_id"
        ),
        {"revision_id": revision_id, "plan_id": plan_id},
    )


async def activate_plan_transactional(
    session: AsyncSession,
    *,
    plan_id: UUID,
    user_id: UUID,
    expected_version: int,
    revision_id: UUID,
    now: datetime,
) -> None:
    """激活计划（§12.2）：CAS + 唯一约束兜底 + revision 转 active。

    IntegrityError（ux_study_plans_one_active）→ ActiveStudyPlanExistsError；
    version CAS 失败 → StudyPlanVersionConflictError。
    """
    if await update_plan_status(
        session,
        plan_id=plan_id,
        expected_version=expected_version,
        new_status="active",
        activated_at=now,
        now=now,
    ):
        await session.execute(
            text(
                "UPDATE study_plan_revisions SET status = 'active', activated_at = :now "
                "WHERE revision_id = :revision_id"
            ),
            {"revision_id": revision_id, "now": now},
        )
        await set_current_revision(session, plan_id=plan_id, revision_id=revision_id)
        return
    # CAS 失败：区分版本冲突与其他
    current = await get_plan_version(session, plan_id=plan_id)
    if current is not None:
        raise StudyPlanVersionConflictError("计划版本冲突，请刷新后重试", current_version=current)
    raise StudyPlanNotFoundError("计划不存在或不属于当前用户")


async def resume_plan_transactional(
    session: AsyncSession,
    *,
    plan_id: UUID,
    user_id: UUID,
    expected_version: int,
    now: datetime,
) -> None:
    """恢复计划（§12.2/D25）：撞 active 唯一约束 → ActiveStudyPlanExistsError。"""
    if await update_plan_status(
        session,
        plan_id=plan_id,
        expected_version=expected_version,
        new_status="active",
        now=now,
    ):
        return
    current = await get_plan_version(session, plan_id=plan_id)
    if current is not None:
        raise StudyPlanVersionConflictError("计划版本冲突，请刷新后重试", current_version=current)
    raise StudyPlanNotFoundError("计划不存在或不属于当前用户")


def is_active_conflict(exc: IntegrityError) -> bool:
    """部分唯一索引 ux_study_plans_one_active 冲突判定（D5 兜底）。"""
    return "ux_study_plans_one_active" in str(exc.orig)


# ---------------------------------------------------------------------------
# 可用时间（§7.3）
# ---------------------------------------------------------------------------


async def insert_availability(
    session: AsyncSession,
    *,
    plan_id: UUID,
    day_of_week: int,
    available_minutes: int,
    start_local_time: str | None,
    end_local_time: str | None,
    is_rest_day: bool,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO study_plan_availability (plan_id, day_of_week, available_minutes,
                start_local_time, end_local_time, is_rest_day)
            VALUES (:plan_id, :day_of_week, :available_minutes, :start_local_time,
                :end_local_time, :is_rest_day)
            """
        ),
        {
            "plan_id": plan_id,
            "day_of_week": day_of_week,
            "available_minutes": available_minutes,
            "start_local_time": start_local_time,
            "end_local_time": end_local_time,
            "is_rest_day": is_rest_day,
        },
    )


async def list_availability(session: AsyncSession, *, plan_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        text("SELECT * FROM study_plan_availability WHERE plan_id = :plan_id ORDER BY day_of_week"),
        {"plan_id": plan_id},
    )
    return [_row(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# Revision（§7.4）
# ---------------------------------------------------------------------------


async def insert_revision(
    session: AsyncSession,
    *,
    revision_id: UUID,
    plan_id: UUID,
    revision_no: int,
    reason: str,
    input_snapshot: dict[str, Any],
    personalization_status: str,
    personalization_reason: str | None,
    change_summary: str | None,
    proposal_operation_id: UUID | None,
    base_revision_id: UUID | None,
    memory_context_hash: str | None = None,
    model_name: str | None = None,
    prompt_version: str | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO study_plan_revisions (revision_id, plan_id, revision_no, reason,
                status, input_snapshot, personalization_status, personalization_reason,
                change_summary, proposal_operation_id, base_revision_id,
                memory_context_hash, model_name, prompt_version)
            VALUES (:revision_id, :plan_id, :revision_no, :reason, 'proposed',
                :input_snapshot, :personalization_status, :personalization_reason,
                :change_summary, :proposal_operation_id, :base_revision_id,
                :memory_context_hash, :model_name, :prompt_version)
            """
        ),
        {
            "revision_id": revision_id,
            "plan_id": plan_id,
            "revision_no": revision_no,
            "reason": reason,
            "input_snapshot": _json(input_snapshot),
            "personalization_status": personalization_status,
            "personalization_reason": personalization_reason,
            "change_summary": change_summary,
            "proposal_operation_id": proposal_operation_id,
            "base_revision_id": base_revision_id,
            "memory_context_hash": memory_context_hash,
            "model_name": model_name,
            "prompt_version": prompt_version,
        },
    )


async def next_revision_no(session: AsyncSession, *, plan_id: UUID) -> int:
    result = await session.execute(
        text(
            "SELECT COALESCE(MAX(revision_no), 0) + 1 FROM study_plan_revisions "
            "WHERE plan_id = :plan_id"
        ),
        {"plan_id": plan_id},
    )
    return int(result.scalar_one())


async def get_revision_row(
    session: AsyncSession, *, plan_id: UUID, revision_id: UUID
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT * FROM study_plan_revisions WHERE revision_id = :revision_id "
            "AND plan_id = :plan_id"
        ),
        {"revision_id": revision_id, "plan_id": plan_id},
    )
    row = result.mappings().first()
    return _row(row) if row is not None else None


async def list_revision_rows(session: AsyncSession, *, plan_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT * FROM study_plan_revisions WHERE plan_id = :plan_id ORDER BY revision_no DESC"
        ),
        {"plan_id": plan_id},
    )
    return [_row(r) for r in result.mappings().all()]


async def update_revision_status(
    session: AsyncSession,
    *,
    revision_id: UUID,
    expected_status: str,
    new_status: str,
    decision_at: datetime,
    decision_actor_id: UUID,
    decision_reason: str | None,
) -> int:
    result = await session.execute(
        text(
            """
            UPDATE study_plan_revisions
            SET status = :new_status, decision_at = :decision_at,
                decision_actor_id = :decision_actor_id,
                decision_reason = :decision_reason
            WHERE revision_id = :revision_id AND status = :expected_status
            """
        ),
        {
            "new_status": new_status,
            "decision_at": decision_at,
            "decision_actor_id": decision_actor_id,
            "decision_reason": decision_reason,
            "revision_id": revision_id,
            "expected_status": expected_status,
        },
    )
    return _rowcount(result)


async def mark_revision_superseded(session: AsyncSession, *, revision_id: UUID) -> None:
    await session.execute(
        text(
            "UPDATE study_plan_revisions SET status = 'superseded' "
            "WHERE revision_id = :revision_id AND status = 'active'"
        ),
        {"revision_id": revision_id},
    )


# ---------------------------------------------------------------------------
# 任务（§7.5/§7.6）
# ---------------------------------------------------------------------------


async def insert_task(
    session: AsyncSession,
    *,
    task_id: UUID,
    plan_id: UUID,
    revision_id: UUID,
    scheduled_date: date,
    order_index: int,
    task_type: str,
    title: str,
    description: str,
    estimated_minutes: int,
    model_estimated_minutes: int | None,
    estimation_basis: str,
    topic_key: str | None,
    source: str,
    source_feed_item_id: UUID | None = None,
    reason_codes: list[str] | None = None,
    graph_node_id: str | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO study_tasks (task_id, plan_id, revision_id, scheduled_date,
                order_index, task_type, title, description, estimated_minutes,
                model_estimated_minutes, estimation_basis, topic_key, graph_node_id,
                reason_codes, source, source_feed_item_id)
            VALUES (:task_id, :plan_id, :revision_id, :scheduled_date, :order_index,
                :task_type, :title, :description, :estimated_minutes,
                :model_estimated_minutes, :estimation_basis, :topic_key, :graph_node_id,
                :reason_codes, :source, :source_feed_item_id)
            """
        ),
        {
            "task_id": task_id,
            "plan_id": plan_id,
            "revision_id": revision_id,
            "scheduled_date": scheduled_date,
            "order_index": order_index,
            "task_type": task_type,
            "title": title,
            "description": description,
            "estimated_minutes": estimated_minutes,
            "model_estimated_minutes": model_estimated_minutes,
            "estimation_basis": estimation_basis,
            "topic_key": topic_key,
            "graph_node_id": graph_node_id,
            "reason_codes": _json(list(reason_codes or [])),
            "source": source,
            "source_feed_item_id": source_feed_item_id,
        },
    )


async def insert_task_event(
    session: AsyncSession,
    *,
    event_id: UUID,
    task_id: UUID,
    event_type: str,
    payload: dict[str, Any] | None = None,
    revision_id: UUID | None = None,
    operation_id: UUID | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO study_task_events (event_id, task_id, event_type, payload,
                revision_id, operation_id)
            VALUES (:event_id, :task_id, :event_type, :payload, :revision_id, :operation_id)
            """
        ),
        {
            "event_id": event_id,
            "task_id": task_id,
            "event_type": event_type,
            "payload": _json(payload or {}),
            "revision_id": revision_id,
            "operation_id": operation_id,
        },
    )


_TASK_SELECT = """
    SELECT t.*, EXISTS (
        SELECT 1 FROM study_task_events e
        WHERE e.task_id = t.task_id AND e.event_type = 'completion_suggested'
          AND NOT EXISTS (
              SELECT 1 FROM study_task_events u
              WHERE u.task_id = t.task_id
                AND u.event_type IN ('completed', 'skipped', 'cancelled')
                AND u.created_at > e.created_at
          )
    ) AS completion_suggestion_pending
    FROM study_tasks t
"""


async def get_task_row(
    session: AsyncSession, *, user_id: UUID, task_id: UUID
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            _TASK_SELECT
            + " JOIN study_plans p ON p.plan_id = t.plan_id "
            + "WHERE t.task_id = :task_id AND p.user_id = :user_id"
        ),
        {"task_id": task_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return _row(row) if row is not None else None


async def list_tasks_for_date(
    session: AsyncSession, *, plan_id: UUID, scheduled_date: date
) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            _TASK_SELECT
            + " WHERE t.plan_id = :plan_id AND t.scheduled_date = :scheduled_date"
            + " AND t.status <> 'cancelled' ORDER BY t.order_index"
        ),
        {"plan_id": plan_id, "scheduled_date": scheduled_date},
    )
    return [_row(r) for r in result.mappings().all()]


async def list_revision_tasks(session: AsyncSession, *, revision_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        text(_TASK_SELECT + " WHERE t.revision_id = :revision_id ORDER BY t.scheduled_date"),
        {"revision_id": revision_id},
    )
    return [_row(r) for r in result.mappings().all()]


async def list_plan_tasks(session: AsyncSession, *, plan_id: UUID) -> list[dict[str, Any]]:
    result = await session.execute(
        text(_TASK_SELECT + " WHERE t.plan_id = :plan_id ORDER BY t.scheduled_date"),
        {"plan_id": plan_id},
    )
    return [_row(r) for r in result.mappings().all()]


async def update_task_status_cas(
    session: AsyncSession,
    *,
    task_id: UUID,
    expected_version: int,
    new_status: str,
    completed_at: datetime | None = None,
    started_at: datetime | None = None,
    completion_source: str | None = None,
) -> int:
    result = await session.execute(
        text(
            """
            UPDATE study_tasks
            SET status = :new_status, version = version + 1,
                completed_at = COALESCE(:completed_at, completed_at),
                started_at = COALESCE(:started_at, started_at),
                completion_source = COALESCE(:completion_source, completion_source)
            WHERE task_id = :task_id AND version = :expected_version
            """
        ),
        {
            "new_status": new_status,
            "completed_at": completed_at,
            "started_at": started_at,
            "completion_source": completion_source,
            "task_id": task_id,
            "expected_version": expected_version,
        },
    )
    return _rowcount(result)


async def update_task_scheduled_date_cas(
    session: AsyncSession,
    *,
    task_id: UUID,
    expected_version: int,
    scheduled_date: date,
) -> int:
    result = await session.execute(
        text(
            "UPDATE study_tasks SET scheduled_date = :scheduled_date, version = version + 1 "
            "WHERE task_id = :task_id AND version = :expected_version"
        ),
        {
            "scheduled_date": scheduled_date,
            "task_id": task_id,
            "expected_version": expected_version,
        },
    )
    return _rowcount(result)


async def get_task_version(session: AsyncSession, *, task_id: UUID) -> int | None:
    result = await session.execute(
        text("SELECT version FROM study_tasks WHERE task_id = :task_id"),
        {"task_id": task_id},
    )
    return result.scalar_one_or_none()


async def sum_tasks_minutes_for_date(
    session: AsyncSession,
    *,
    plan_id: UUID,
    scheduled_date: date,
    exclude_task_id: UUID | None = None,
) -> tuple[int, int]:
    """某日非取消任务分钟与完成分钟（reschedule 碰撞检测用，§12.3）。"""
    result = await session.execute(
        text(
            """
            SELECT COALESCE(SUM(estimated_minutes), 0) AS planned,
                   COALESCE(SUM(estimated_minutes) FILTER (WHERE status = 'completed'), 0)
                   AS completed
            FROM study_tasks
            WHERE plan_id = :plan_id AND scheduled_date = :scheduled_date
              AND status <> 'cancelled'
              AND task_id <> COALESCE(:exclude, '00000000-0000-0000-0000-000000000000')
            """
        ),
        {
            "plan_id": plan_id,
            "scheduled_date": scheduled_date,
            "exclude": exclude_task_id,
        },
    )
    row = result.mappings().first()
    assert row is not None
    return int(row["planned"]), int(row["completed"])


async def cancel_tasks_for_plan_lifecycle(session: AsyncSession, *, plan_id: UUID) -> list[UUID]:
    """计划归档时取消非终态任务（§12.3 矩阵末行），返回受影响任务。"""
    result = await session.execute(
        text(
            """
            UPDATE study_tasks
            SET status = 'cancelled', version = version + 1
            WHERE plan_id = :plan_id AND status IN ('pending', 'in_progress', 'skipped')
            RETURNING task_id
            """
        ),
        {"plan_id": plan_id},
    )
    return [UUID(str(r[0])) for r in result.all()]


def _conflict(exc: IntegrityError, fragment: str) -> bool:
    return fragment in str(exc.orig)


# ---------------------------------------------------------------------------
# Session（§7.9）
# ---------------------------------------------------------------------------


async def insert_session(
    session: AsyncSession,
    *,
    session_id: UUID,
    user_id: UUID,
    task_id: UUID,
    conversation_status: str,
    conversation_create_request_id: str | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO study_sessions (session_id, user_id, task_id, conversation_status,
                conversation_create_request_id)
            VALUES (:session_id, :user_id, :task_id, :conversation_status,
                :conversation_create_request_id)
            """
        ),
        {
            "session_id": session_id,
            "user_id": user_id,
            "task_id": task_id,
            "conversation_status": conversation_status,
            "conversation_create_request_id": conversation_create_request_id,
        },
    )


async def find_active_session_for_task(
    session: AsyncSession, *, task_id: UUID
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT * FROM study_sessions WHERE task_id = :task_id AND status = 'active' "
            "ORDER BY started_at DESC LIMIT 1"
        ),
        {"task_id": task_id},
    )
    row = result.mappings().first()
    return _row(row) if row is not None else None


async def get_session_row(
    session: AsyncSession, *, user_id: UUID, session_id: UUID
) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM study_sessions WHERE session_id = :session_id AND user_id = :user_id"),
        {"session_id": session_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return _row(row) if row is not None else None


async def update_session_heartbeat(
    session: AsyncSession,
    *,
    session_id: UUID,
    seq: int,
    now: datetime,
    added_seconds: int,
) -> int:
    """heartbeat 落库：seq 递增 CAS（同 seq 由上层幂等短路，不进这里）。"""
    result = await session.execute(
        text(
            """
            UPDATE study_sessions
            SET last_heartbeat_at = :now, last_heartbeat_seq = :seq,
                active_seconds = active_seconds + :added
            WHERE session_id = :session_id AND status = 'active'
              AND last_heartbeat_seq < :seq
            """
        ),
        {
            "now": now,
            "seq": seq,
            "added": added_seconds,
            "session_id": session_id,
        },
    )
    return _rowcount(result)


async def update_session_finish(
    session: AsyncSession,
    *,
    session_id: UUID,
    new_status: str,
    now: datetime,
    added_seconds: int,
) -> int:
    result = await session.execute(
        text(
            """
            UPDATE study_sessions
            SET status = :new_status, ended_at = :now,
                active_seconds = active_seconds + :added
            WHERE session_id = :session_id AND status = 'active'
            """
        ),
        {"new_status": new_status, "now": now, "added": added_seconds, "session_id": session_id},
    )
    return _rowcount(result)


async def upsert_daily_stats_add_activity(
    session: AsyncSession,
    *,
    user_id: UUID,
    local_date: date,
    active_seconds: int,
    session_count: int,
) -> None:
    """§7.10：按 (user_id, local_date) 幂等累加（ON CONFLICT 覆盖更新）。"""
    await session.execute(
        text(
            """
            INSERT INTO study_daily_stats (user_id, local_date, active_seconds,
                completed_task_count, session_count)
            VALUES (:user_id, :local_date, :active_seconds, 0, :session_count)
            ON CONFLICT (user_id, local_date) DO UPDATE
            SET active_seconds = study_daily_stats.active_seconds + :active_seconds,
                session_count = study_daily_stats.session_count + :session_count,
                updated_at = now()
            """
        ),
        {
            "user_id": user_id,
            "local_date": local_date,
            "active_seconds": active_seconds,
            "session_count": session_count,
        },
    )


async def increment_daily_completed(
    session: AsyncSession, *, user_id: UUID, local_date: date
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO study_daily_stats (user_id, local_date, active_seconds,
                completed_task_count, session_count)
            VALUES (:user_id, :local_date, 0, 1, 0)
            ON CONFLICT (user_id, local_date) DO UPDATE
            SET completed_task_count = study_daily_stats.completed_task_count + 1,
                updated_at = now()
            """
        ),
        {"user_id": user_id, "local_date": local_date},
    )


async def get_daily_stats_between(
    session: AsyncSession, *, user_id: UUID, from_date: date, to_date: date
) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT * FROM study_daily_stats WHERE user_id = :user_id "
            "AND local_date BETWEEN :from_date AND :to_date ORDER BY local_date"
        ),
        {"user_id": user_id, "from_date": from_date, "to_date": to_date},
    )
    return [_row(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# Operation（§7.12/§12.7）
# ---------------------------------------------------------------------------


async def insert_operation(
    session: AsyncSession,
    *,
    operation_id: UUID,
    user_id: UUID,
    operation_type: str,
    payload: dict[str, Any] | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO study_operations (operation_id, user_id, operation_type, payload)
            VALUES (:operation_id, :user_id, :operation_type, :payload)
            """
        ),
        {
            "operation_id": operation_id,
            "user_id": user_id,
            "operation_type": operation_type,
            "payload": _json(payload or {}),
        },
    )


async def get_operation_row(
    session: AsyncSession, *, user_id: UUID, operation_id: UUID
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT * FROM study_operations WHERE operation_id = :operation_id "
            "AND user_id = :user_id"
        ),
        {"operation_id": operation_id, "user_id": user_id},
    )
    row = result.mappings().first()
    return _row(row) if row is not None else None


async def update_operation_status(
    session: AsyncSession,
    *,
    operation_id: UUID,
    expected_status: str | None,
    new_status: str,
    result_payload: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> int:
    expected_clause = "status = :expected_status" if expected_status else "TRUE"
    result = await session.execute(
        text(
            f"""
            UPDATE study_operations
            SET status = :new_status, result = :result_payload,
                error_code = :error_code, error_message = :error_message, updated_at = now()
            WHERE operation_id = :operation_id AND {expected_clause}
            """
        ),
        {
            "new_status": new_status,
            "result_payload": _json(result_payload),
            "error_code": error_code,
            "error_message": error_message,
            "operation_id": operation_id,
            "expected_status": expected_status,
        },
    )
    return _rowcount(result)


# ---------------------------------------------------------------------------
# 幂等（§7.12/D16）
# ---------------------------------------------------------------------------


async def get_idempotency_row(
    session: AsyncSession,
    *,
    user_id: UUID,
    operation_name: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT * FROM study_idempotency_requests WHERE user_id = :user_id "
            "AND operation_name = :operation_name AND idempotency_key = :idempotency_key"
        ),
        {
            "user_id": user_id,
            "operation_name": operation_name,
            "idempotency_key": idempotency_key,
        },
    )
    row = result.mappings().first()
    return _row(row) if row is not None else None


async def insert_idempotency_row(
    session: AsyncSession,
    *,
    idempotency_request_id: UUID,
    user_id: UUID,
    operation_name: str,
    idempotency_key: str,
    request_hash: str,
    expires_at: datetime,
    operation_id: UUID | None = None,
) -> bool:
    """插入幂等记录；唯一键冲突返回 False（并发重放，由上层重查）。"""
    try:
        await session.execute(
            text(
                """
                INSERT INTO study_idempotency_requests (idempotency_request_id, user_id,
                    operation_name, idempotency_key, request_hash, operation_id, expires_at)
                VALUES (:idempotency_request_id, :user_id, :operation_name, :idempotency_key,
                    :request_hash, :operation_id, :expires_at)
                """
            ),
            {
                "idempotency_request_id": idempotency_request_id,
                "user_id": user_id,
                "operation_name": operation_name,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
                "operation_id": operation_id,
                "expires_at": expires_at,
            },
        )
        return True
    except IntegrityError:
        return False


async def update_idempotency_result(
    session: AsyncSession,
    *,
    user_id: UUID,
    operation_name: str,
    idempotency_key: str,
    response_status: int,
    response_body: dict[str, Any] | None,
    operation_id: UUID | None = None,
) -> None:
    await session.execute(
        text(
            """
            UPDATE study_idempotency_requests
            SET response_status = :response_status, response_body = :response_body,
                operation_id = COALESCE(:operation_id, operation_id)
            WHERE user_id = :user_id AND operation_name = :operation_name
              AND idempotency_key = :idempotency_key
            """
        ),
        {
            "response_status": response_status,
            "response_body": _json(response_body),
            "operation_id": operation_id,
            "user_id": user_id,
            "operation_name": operation_name,
            "idempotency_key": idempotency_key,
        },
    )


# ---------------------------------------------------------------------------
# Purge（§12.8/D19）
# ---------------------------------------------------------------------------

PURGE_TABLE_ORDER: tuple[str, ...] = (
    "study_account_purge_ledger",
    "study_user_leases",
    "study_idempotency_requests",
    "study_outbox",
    "study_operations",
    "study_model_call_records",
    "study_daily_stats",
    "study_sessions",
    "study_daily_feed_items",
    "study_daily_feed_runs",
    "study_task_events",
    "study_tasks",
    "study_plan_revisions",
    "study_plan_availability",
    "study_plans",
    "study_plan_intakes",
)


async def purge_user_data(session: AsyncSession, *, user_id: UUID) -> int:
    """删除某用户全部 Study 数据（§18.9：覆盖全部表，返回删除行数）。

    无 user_id 列的表按归属关系级联定位：feed_items 走 feed_runs、
    task_events 走 tasks、tasks/revisions/availability 走 plans。
    study_scheduler_runs 是用户无关的维护记录，不参与清理。
    """
    count = 0

    async def _delete(sql: str, params: dict[str, Any]) -> None:
        nonlocal count
        count += _rowcount(await session.execute(text(sql), params))

    await _delete(
        "DELETE FROM study_daily_feed_items WHERE feed_run_id IN "
        "(SELECT feed_run_id FROM study_daily_feed_runs WHERE user_id = :uid)",
        {"uid": user_id},
    )
    await _delete(
        "DELETE FROM study_task_events WHERE task_id IN "
        "(SELECT task_id FROM study_tasks WHERE plan_id IN "
        "(SELECT plan_id FROM study_plans WHERE user_id = :uid))",
        {"uid": user_id},
    )
    await _delete(
        "DELETE FROM study_tasks WHERE plan_id IN "
        "(SELECT plan_id FROM study_plans WHERE user_id = :uid)",
        {"uid": user_id},
    )
    await _delete(
        "DELETE FROM study_plan_revisions WHERE plan_id IN "
        "(SELECT plan_id FROM study_plans WHERE user_id = :uid)",
        {"uid": user_id},
    )
    await _delete(
        "DELETE FROM study_plan_availability WHERE plan_id IN "
        "(SELECT plan_id FROM study_plans WHERE user_id = :uid)",
        {"uid": user_id},
    )
    for table in (
        "study_account_purge_ledger",
        "study_user_leases",
        "study_idempotency_requests",
        "study_outbox",
        "study_operations",
        "study_model_call_records",
        "study_daily_stats",
        "study_sessions",
        "study_daily_feed_runs",
        "study_plans",
        "study_plan_intakes",
    ):
        await _delete(f"DELETE FROM {table} WHERE user_id = :uid", {"uid": user_id})
    return count


def idempotency_expiry(now: datetime, retention_days: int) -> datetime:
    return now + timedelta(days=retention_days)


def model_cache_expiry(now: datetime, retention_days: int) -> datetime:
    return now + timedelta(days=retention_days)
