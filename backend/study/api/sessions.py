"""Study Session API（§12.5/D23/D24/D28）。

第一版不提供 POST /sessions：Session 只能由 task start/launch 创建或复用（D23）。
heartbeat 以 (session_id, seq) 自身为幂等锚，不要求 Idempotency-Key（§15.1）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from backend.auth.context import AuthContext
from backend.shared.auth_context import get_auth_context
from backend.study.api.dependencies import StudyRuntimeDep, StudySessionDep
from backend.study.contracts.api import HeartbeatRequest, SessionOut
from backend.study.contracts.errors import StudySessionConflictError
from backend.study.persistence import repositories as repo
from backend.study.services import session_service

router = APIRouter()


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_session(
    session_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
) -> SessionOut:
    """只读返回 Session、conversation_status 与可能已回填的 thread id（§12.5）。"""
    row = await repo.get_session_row(session, user_id=auth.user_id, session_id=session_id)
    if row is None:
        raise StudySessionConflictError("Session 不存在或不属于当前用户")
    return SessionOut.model_validate(row)


@router.post("/sessions/{session_id}/heartbeat", response_model=SessionOut)
async def heartbeat(
    session_id: UUID,
    body: HeartbeatRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
) -> SessionOut:
    """§12.5：单调 seq；相同 seq 幂等重放，乱序 409，过快 429 RATE_LIMITED。"""
    row = await repo.get_session_row(session, user_id=auth.user_id, session_id=session_id)
    if row is None:
        raise StudySessionConflictError("Session 不存在或不属于当前用户")
    refreshed = await session_service.heartbeat(
        session,
        session_row=row,
        seq=body.seq,
        now=datetime.now().astimezone(),
        min_interval_seconds=runtime.settings.study_session_heartbeat_min_interval_seconds,
        idle_timeout_seconds=runtime.settings.study_session_idle_timeout_seconds,
    )
    await session.commit()
    return SessionOut.model_validate(refreshed)


@router.post("/sessions/{session_id}/finish", response_model=SessionOut)
async def finish_session(
    session_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    """结束 Session（§12.5：结算最后有效区间，写入 daily_stats）。"""
    from backend.study.services.idempotency import (
        open_idempotent_request,
        record_idempotent_result,
    )

    opened = await open_idempotent_request(
        session,
        user_id=auth.user_id,
        operation_name="session.finish",
        idempotency_key=idempotency_key,
        payload={"session_id": str(session_id)},
        now=datetime.now().astimezone(),
        retention_days=runtime.settings.study_idempotency_retention_days,
    )
    if opened.replay and opened.response_status is not None:
        return JSONResponse(status_code=opened.response_status, content=opened.response_body or {})
    row = await repo.get_session_row(session, user_id=auth.user_id, session_id=session_id)
    if row is None:
        raise StudySessionConflictError("Session 不存在或不属于当前用户")
    plan_row = None
    if row["task_id"] is not None:
        from backend.study.services import task_service

        task = await task_service.get_task(
            session, user_id=auth.user_id, task_id=UUID(str(row["task_id"]))
        )
        plan_row = await repo.get_plan_row(
            session, user_id=auth.user_id, plan_id=UUID(str(task["plan_id"]))
        )
    timezone = str(plan_row["timezone"]) if plan_row is not None else "UTC"
    refreshed = await session_service.finish_session(
        session,
        session_row=row,
        now=datetime.now().astimezone(),
        idle_timeout_seconds=runtime.settings.study_session_idle_timeout_seconds,
        plan_timezone=timezone,
    )
    content = SessionOut.model_validate(refreshed).model_dump(mode="json")
    await record_idempotent_result(
        session,
        user_id=auth.user_id,
        operation_name="session.finish",
        idempotency_key=idempotency_key,
        response_status=200,
        response_body=content,
    )
    await session.commit()
    return JSONResponse(status_code=200, content=content)
