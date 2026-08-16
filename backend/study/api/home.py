"""Study 首页 API（§12.6/D9/D22，v1.2：GET 无副作用，未来日期 422）。

- date 缺省由服务端按 active plan IANA 时区计算；调用方提交的日期不得晚于
  服务端判定的今天（§12.6，不信任浏览器时钟）；
- generation_status：当天 feed run 已成功且 input_hash 匹配 → ready，
  否则 pending；无 active plan → no_active_plan；
- POST /home/ensure-today：显式触发唯一 feed run/operation（D9），
  无 active plan → 409 STUDY_NO_ACTIVE_PLAN 零副作用（D22）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from backend.auth.context import AuthContext
from backend.shared.auth_context import get_auth_context
from backend.study.api.dependencies import StudyRuntime, StudyRuntimeDep, StudySessionDep
from backend.study.contracts.api import EnsureTodayOut, HomeOut
from backend.study.persistence import repositories as repo
from backend.study.services import feed_service
from backend.study.services.home_service import aggregate_home, server_today
from backend.study.services.idempotency import (
    open_idempotent_request,
    record_idempotent_result,
)

router = APIRouter()


async def _generation_status(
    session: StudySessionDep,
    *,
    runtime: StudyRuntime,
    user_id: UUID,
    plan: dict[str, Any] | None,
    local_date: date,
) -> tuple[str, list[dict[str, Any]]]:
    """§12.6：feed run 状态判定 + 当日 active 推荐。"""
    if plan is None:
        return "no_active_plan", []
    row = (
        (
            await session.execute(
                text(
                    "SELECT * FROM study_daily_feed_runs WHERE user_id = :user_id "
                    "AND plan_id = :plan_id AND local_date = :local_date"
                ),
                {"user_id": user_id, "plan_id": plan["plan_id"], "local_date": local_date},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        return "pending", []
    current_hash = feed_service.feed_input_hash(
        plan_id=UUID(str(plan["plan_id"])),
        revision_id=plan["current_revision_id"],
        local_date=local_date,
        daily_feed_enabled=bool(runtime.settings.study_daily_feed_enabled),
        memory_read_enabled=bool(runtime.settings.study_memory_read_enabled),
    )
    if row["status"] == "succeeded" and feed_service.feed_run_matches_deterministic(
        row["input_hash"], current_hash
    ):
        items = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM study_daily_feed_items WHERE feed_run_id = :run_id "
                        "AND status = 'active' AND source_type = 'recommendation' "
                        "ORDER BY created_at"
                    ),
                    {"run_id": row["feed_run_id"]},
                )
            )
            .mappings()
            .all()
        )
        return "ready", [dict(i) for i in items]
    return "pending", []


@router.get("/home", response_model=HomeOut)
async def get_home(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    date_param: Annotated[date | None, Query(alias="date")] = None,
) -> HomeOut:
    """§12.6：只读聚合；future date → 422 INVALID_PAYLOAD。"""
    plan = await repo.get_active_plan_row(session, user_id=auth.user_id)
    timezone = (
        str(plan["timezone"]) if plan is not None else runtime.settings.study_scheduler_timezone
    )
    today = server_today(timezone)
    local_date = date_param or today
    if local_date > today:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("query", "date"),
                    "msg": "date 不得晚于服务端判定的今天",
                    "input": date_param,
                }
            ]
        )
    home = await aggregate_home(session, user_id=auth.user_id, local_date=local_date)
    status, recommendations = await _generation_status(
        session, runtime=runtime, user_id=auth.user_id, plan=plan, local_date=local_date
    )
    home["today"]["generation_status"] = status
    home["today"]["recommendations"] = [
        {
            "feed_item_id": str(r["feed_item_id"]),
            "title": r["title"],
            "reason": r["reason"],
            "reason_codes": r["reason_codes"],
            "topic_key": r["topic_key"],
            "graph_node_id": r["graph_node_id"],
            "estimated_minutes": r["estimated_minutes"],
            "status": r["status"],
        }
        for r in recommendations[:2]  # 验收 #10：最多两条额外自适应推荐
    ]
    return HomeOut.model_validate(home)


@router.post("/home/ensure-today", response_model=EnsureTodayOut)
async def ensure_today(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    """D9/D22：显式触发当日 feed（业务键 (user_id, plan_id, local_date)）。"""
    plan = await repo.get_active_plan_row(session, user_id=auth.user_id)
    timezone = (
        str(plan["timezone"]) if plan is not None else runtime.settings.study_scheduler_timezone
    )
    local_date = server_today(timezone)
    opened = await open_idempotent_request(
        session,
        user_id=auth.user_id,
        operation_name="home.ensure_today",
        idempotency_key=idempotency_key,
        payload={"local_date": local_date.isoformat()},
        now=datetime.now().astimezone(),
        retention_days=runtime.settings.study_idempotency_retention_days,
    )
    if opened.replay and opened.response_status is not None:
        return JSONResponse(status_code=opened.response_status, content=opened.response_body or {})
    run_id, operation_id = await feed_service.ensure_daily_feed(
        session, user_id=auth.user_id, local_date=local_date, settings=runtime.settings
    )
    content = {
        "feed_run_id": str(run_id),
        "operation_id": str(operation_id) if operation_id else None,
        "generation_status": "ready" if operation_id is None else "queued",
    }
    await record_idempotent_result(
        session,
        user_id=auth.user_id,
        operation_name="home.ensure_today",
        idempotency_key=idempotency_key,
        response_status=202 if operation_id else 200,
        response_body=content,
        operation_id=operation_id,
    )
    await session.commit()
    return JSONResponse(status_code=202 if operation_id else 200, content=content)
