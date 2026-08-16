"""Study 任务 API（§12.3/D11/D13/D24/D27/D28）。

所有写接口必须携带 Idempotency-Key 与 expected_version（§15.1/§15.3）。
launch 返回稳定响应骨架（§12.5：conversation_status=pending，thread 回填 Phase 3）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from backend.auth.context import AuthContext
from backend.shared.auth_context import get_auth_context
from backend.study.api.dependencies import StudyRuntime, StudyRuntimeDep, StudySessionDep
from backend.study.contracts.api import (
    LaunchOut,
    TaskActionRequest,
    TaskOut,
    TaskRescheduleRequest,
)
from backend.study.persistence import repositories as repo
from backend.study.services import task_service
from backend.study.services.home_service import server_today
from backend.study.services.idempotency import (
    open_idempotent_request,
    record_idempotent_result,
)

router = APIRouter()


async def _task_with_plan(
    session: StudySessionDep, *, user_id: UUID, task_id: UUID
) -> tuple[dict[str, Any], dict[str, Any]]:
    task = await task_service.get_task(session, user_id=user_id, task_id=task_id)
    plan = await repo.get_plan_row(session, user_id=user_id, plan_id=UUID(str(task["plan_id"])))
    assert plan is not None
    return task, plan


async def _replay(
    session: StudySessionDep,
    *,
    runtime: StudyRuntime,
    user_id: UUID,
    operation_name: str,
    key: str,
    payload: object,
) -> JSONResponse | None:
    opened = await open_idempotent_request(
        session,
        user_id=user_id,
        operation_name=operation_name,
        idempotency_key=key,
        payload=payload,
        now=datetime.now().astimezone(),
        retention_days=runtime.settings.study_idempotency_retention_days,
    )
    if opened.replay and opened.response_status is not None:
        return JSONResponse(status_code=opened.response_status, content=opened.response_body or {})
    return None


async def _task_action(
    session: StudySessionDep,
    *,
    runtime: StudyRuntime,
    auth: AuthContext,
    task_id: UUID,
    expected_version: int,
    action: str,
    operation_name: str,
    key: str,
) -> JSONResponse:
    replay = await _replay(
        session,
        runtime=runtime,
        user_id=auth.user_id,
        operation_name=operation_name,
        key=key,
        payload={"task_id": str(task_id), "expected_version": expected_version},
    )
    if replay is not None:
        return replay
    task, plan = await _task_with_plan(session, user_id=auth.user_id, task_id=task_id)
    now = datetime.now().astimezone()
    idle_timeout = runtime.settings.study_session_idle_timeout_seconds
    if action == "start":
        row = await task_service.start_task(
            session,
            task_row=task,
            user_id=auth.user_id,
            expected_version=expected_version,
            now=now,
        )
    elif action == "complete":
        row = await task_service.complete_task(
            session,
            task_row=task,
            expected_version=expected_version,
            now=now,
            plan_timezone=str(plan["timezone"]),
            idle_timeout=idle_timeout,
            memory_writeback=runtime.settings.study_memory_writeback_enabled,
        )
    elif action == "reopen":
        row = await task_service.reopen_task(
            session,
            task_row=task,
            expected_version=expected_version,
        )
    else:  # skip
        row = await task_service.skip_task(
            session,
            task_row=task,
            expected_version=expected_version,
            now=now,
            plan_timezone=str(plan["timezone"]),
            idle_timeout=idle_timeout,
        )
    content = TaskOut.model_validate(row).model_dump(mode="json")
    await record_idempotent_result(
        session,
        user_id=auth.user_id,
        operation_name=operation_name,
        idempotency_key=key,
        response_status=200,
        response_body=content,
    )
    await session.commit()
    return JSONResponse(status_code=200, content=content)


@router.post("/tasks/{task_id}/start")
async def start_task(
    task_id: UUID,
    body: TaskActionRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    return await _task_action(
        session,
        runtime=runtime,
        auth=auth,
        task_id=task_id,
        expected_version=body.expected_version,
        action="start",
        operation_name="task.start",
        key=idempotency_key,
    )


@router.post("/tasks/{task_id}/complete")
async def complete_task(
    task_id: UUID,
    body: TaskActionRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    return await _task_action(
        session,
        runtime=runtime,
        auth=auth,
        task_id=task_id,
        expected_version=body.expected_version,
        action="complete",
        operation_name="task.complete",
        key=idempotency_key,
    )


@router.post("/tasks/{task_id}/reopen")
async def reopen_task(
    task_id: UUID,
    body: TaskActionRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    return await _task_action(
        session,
        runtime=runtime,
        auth=auth,
        task_id=task_id,
        expected_version=body.expected_version,
        action="reopen",
        operation_name="task.reopen",
        key=idempotency_key,
    )


@router.post("/tasks/{task_id}/skip")
async def skip_task(
    task_id: UUID,
    body: TaskActionRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    return await _task_action(
        session,
        runtime=runtime,
        auth=auth,
        task_id=task_id,
        expected_version=body.expected_version,
        action="skip",
        operation_name="task.skip",
        key=idempotency_key,
    )


@router.post("/tasks/{task_id}/reschedule")
async def reschedule_task(
    task_id: UUID,
    body: TaskRescheduleRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    payload = {
        "task_id": str(task_id),
        "scheduled_date": body.scheduled_date.isoformat(),
        "expected_version": body.expected_version,
    }
    replay = await _replay(
        session,
        runtime=runtime,
        user_id=auth.user_id,
        operation_name="task.reschedule",
        key=idempotency_key,
        payload=payload,
    )
    if replay is not None:
        return replay
    task, plan = await _task_with_plan(session, user_id=auth.user_id, task_id=task_id)
    today = server_today(str(plan["timezone"]))
    row = await task_service.reschedule_task(
        session,
        task_row=task,
        plan_row=plan,
        expected_version=body.expected_version,
        scheduled_date=body.scheduled_date,
        today_local=today,
    )
    content = TaskOut.model_validate(row).model_dump(mode="json")
    await record_idempotent_result(
        session,
        user_id=auth.user_id,
        operation_name="task.reschedule",
        idempotency_key=idempotency_key,
        response_status=200,
        response_body=content,
    )
    await session.commit()
    return JSONResponse(status_code=200, content=content)


@router.post("/tasks/{task_id}/launch", response_model=LaunchOut)
async def launch_task(
    task_id: UUID,
    body: TaskActionRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    replay = await _replay(
        session,
        runtime=runtime,
        user_id=auth.user_id,
        operation_name="task.launch",
        key=idempotency_key,
        payload={"task_id": str(task_id), "expected_version": body.expected_version},
    )
    if replay is not None:
        return replay
    task, _plan = await _task_with_plan(session, user_id=auth.user_id, task_id=task_id)
    _task_row, launch = await task_service.launch_task(
        session,
        task_row=task,
        user_id=auth.user_id,
        expected_version=body.expected_version,
        now=datetime.now().astimezone(),
    )
    content = LaunchOut.model_validate(launch).model_dump(mode="json")
    await record_idempotent_result(
        session,
        user_id=auth.user_id,
        operation_name="task.launch",
        idempotency_key=idempotency_key,
        response_status=200,
        response_body=content,
    )
    await session.commit()
    return JSONResponse(status_code=200, content=content)
