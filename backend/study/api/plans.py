"""Study 计划 API（§12.2/D8/D21/D25/D26）。

- POST /plans：manual 直录同步出草案（201）/ ai 提交 operation（202）；
- 生命周期 activate/pause/resume/archive：Idempotency-Key + expected_version；
- calendar 只读返回完整计划日期范围（含休息日/空任务日）；
- revisions 列表 + accept/reject 决策（CAS + operation 终态）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from backend.auth.context import AuthContext
from backend.shared.auth_context import get_auth_context
from backend.study.api.dependencies import StudyRuntime, StudyRuntimeDep, StudySessionDep
from backend.study.contracts.api import (
    CalendarOut,
    PlanActivateRequest,
    PlanCreateRequest,
    PlanOut,
    RevisionDecisionRequest,
    RevisionOut,
)
from backend.study.persistence import repositories as repo
from backend.study.services import plan_service
from backend.study.services.idempotency import (
    open_idempotent_request,
    record_idempotent_result,
)

router = APIRouter()


def _now() -> datetime:
    return datetime.now().astimezone()


async def _replay_or_none(
    session: StudySessionDep,
    *,
    user_id: UUID,
    operation_name: str,
    key: str,
    payload: object,
    runtime: StudyRuntime,
) -> JSONResponse | None:
    opened = await open_idempotent_request(
        session,
        user_id=user_id,
        operation_name=operation_name,
        idempotency_key=key,
        payload=payload,
        now=_now(),
        retention_days=runtime.settings.study_idempotency_retention_days,
    )
    if opened.replay and opened.response_status is not None:
        return JSONResponse(
            status_code=opened.response_status,
            content=opened.response_body or {},
        )
    return None


@router.post("/plans", status_code=201)
async def create_plan(
    body: PlanCreateRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    request: Request,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    """创建计划（§12.2）。manual → 201 + 草案；ai → 202 + operation_id。"""
    payload = {
        "generation_mode": body.generation_mode,
        "intent": body.intent.model_dump(mode="json"),
        "task_blueprint": [b.model_dump(mode="json") for b in body.task_blueprint],
    }
    replay = await _replay_or_none(
        session,
        user_id=auth.user_id,
        operation_name="plan.create",
        key=idempotency_key,
        payload=payload,
        runtime=runtime,
    )
    if replay is not None:
        return replay
    result = await plan_service.create_plan(
        session, user_id=auth.user_id, request=body, settings=runtime.settings
    )
    if result["mode"] == "ai":
        response = JSONResponse(
            status_code=202,
            content={"operation_id": str(result["operation_id"]), "status": "queued"},
        )
        await record_idempotent_result(
            session,
            user_id=auth.user_id,
            operation_name="plan.create",
            idempotency_key=idempotency_key,
            response_status=202,
            response_body={"operation_id": str(result["operation_id"]), "status": "queued"},
            operation_id=result["operation_id"],
        )
        await session.commit()
        return response
    plan_out = PlanOut.model_validate(result["plan"]).model_dump(mode="json")
    response = JSONResponse(status_code=201, content=plan_out)
    await record_idempotent_result(
        session,
        user_id=auth.user_id,
        operation_name="plan.create",
        idempotency_key=idempotency_key,
        response_status=201,
        response_body=plan_out,
    )
    await session.commit()
    return response


@router.get("/plans", response_model=list[PlanOut])
async def list_plans(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
) -> list[PlanOut]:
    rows = await repo.list_plan_rows(session, user_id=auth.user_id)
    return [PlanOut.model_validate(r) for r in rows]


@router.get("/plans/{plan_id}", response_model=PlanOut)
async def get_plan(
    plan_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
) -> PlanOut:
    plan = await repo.get_plan_row(session, user_id=auth.user_id, plan_id=plan_id)
    if plan is None:
        from backend.study.contracts.errors import StudyPlanNotFoundError

        raise StudyPlanNotFoundError("计划不存在或不属于当前用户")
    return PlanOut.model_validate(plan)


@router.get("/plans/{plan_id}/calendar", response_model=CalendarOut)
async def get_calendar(
    plan_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
) -> CalendarOut:
    """§12.2/D26：只读、无副作用，覆盖完整计划日期范围。"""
    calendar = await plan_service.get_calendar(session, user_id=auth.user_id, plan_id=plan_id)
    return CalendarOut.model_validate(calendar)


@router.get("/plans/{plan_id}/revisions", response_model=list[RevisionOut])
async def list_revisions(
    plan_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
) -> list[RevisionOut]:
    plan = await repo.get_plan_row(session, user_id=auth.user_id, plan_id=plan_id)
    if plan is None:
        from backend.study.contracts.errors import StudyPlanNotFoundError

        raise StudyPlanNotFoundError("计划不存在或不属于当前用户")
    rows = await repo.list_revision_rows(session, plan_id=plan_id)
    return [RevisionOut.model_validate(r) for r in rows]


async def _lifecycle(
    session: StudySessionDep,
    *,
    user_id: UUID,
    plan_id: UUID,
    expected_version: int,
    action: str,
    memory_writeback: bool = False,
) -> PlanOut:
    if action == "activate":
        plan = await plan_service.activate_plan(
            session,
            user_id=user_id,
            plan_id=plan_id,
            expected_version=expected_version,
            memory_writeback=memory_writeback,
        )
    elif action == "pause":
        plan = await plan_service.pause_plan(
            session, user_id=user_id, plan_id=plan_id, expected_version=expected_version
        )
    elif action == "resume":
        plan = await plan_service.resume_plan(
            session, user_id=user_id, plan_id=plan_id, expected_version=expected_version
        )
    else:
        plan = await plan_service.archive_plan(
            session, user_id=user_id, plan_id=plan_id, expected_version=expected_version
        )
    return PlanOut.model_validate(plan)


# lifecycle 端点单独实现（幂等键来自 Header，见下方四个路由）


@router.post("/plans/{plan_id}/activate")
async def activate_plan(
    plan_id: UUID,
    body: PlanActivateRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    return await _lifecycle_call(
        session,
        runtime=runtime,
        auth=auth,
        plan_id=plan_id,
        expected_version=body.expected_version,
        action="activate",
        operation_name="plan.activate",
        key=idempotency_key,
    )


@router.post("/plans/{plan_id}/pause")
async def pause_plan(
    plan_id: UUID,
    body: PlanActivateRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    return await _lifecycle_call(
        session,
        runtime=runtime,
        auth=auth,
        plan_id=plan_id,
        expected_version=body.expected_version,
        action="pause",
        operation_name="plan.pause",
        key=idempotency_key,
    )


@router.post("/plans/{plan_id}/resume")
async def resume_plan(
    plan_id: UUID,
    body: PlanActivateRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    return await _lifecycle_call(
        session,
        runtime=runtime,
        auth=auth,
        plan_id=plan_id,
        expected_version=body.expected_version,
        action="resume",
        operation_name="plan.resume",
        key=idempotency_key,
    )


@router.post("/plans/{plan_id}/archive")
async def archive_plan(
    plan_id: UUID,
    body: PlanActivateRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    return await _lifecycle_call(
        session,
        runtime=runtime,
        auth=auth,
        plan_id=plan_id,
        expected_version=body.expected_version,
        action="archive",
        operation_name="plan.archive",
        key=idempotency_key,
    )


async def _lifecycle_call(
    session: StudySessionDep,
    *,
    runtime: StudyRuntime,
    auth: AuthContext,
    plan_id: UUID,
    expected_version: int,
    action: str,
    operation_name: str,
    key: str,
) -> JSONResponse:
    replay = await _replay_or_none(
        session,
        user_id=auth.user_id,
        operation_name=operation_name,
        key=key,
        payload={"plan_id": str(plan_id), "expected_version": expected_version},
        runtime=runtime,
    )
    if replay is not None:
        return replay
    plan = await _lifecycle(
        session,
        user_id=auth.user_id,
        plan_id=plan_id,
        expected_version=expected_version,
        action=action,
        memory_writeback=runtime.settings.study_memory_writeback_enabled,
    )
    content = plan.model_dump(mode="json")
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


@router.post("/plans/{plan_id}/revisions/{revision_id}/accept")
async def accept_revision(
    plan_id: UUID,
    revision_id: UUID,
    body: RevisionDecisionRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    return await _revision_decision(
        session,
        runtime=runtime,
        auth=auth,
        plan_id=plan_id,
        revision_id=revision_id,
        expected_version=body.expected_version,
        decision="accept",
        reason=body.reason,
        operation_name="revision.accept",
        key=idempotency_key,
    )


@router.post("/plans/{plan_id}/revisions/{revision_id}/reject")
async def reject_revision(
    plan_id: UUID,
    revision_id: UUID,
    body: RevisionDecisionRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    return await _revision_decision(
        session,
        runtime=runtime,
        auth=auth,
        plan_id=plan_id,
        revision_id=revision_id,
        expected_version=body.expected_version,
        decision="reject",
        reason=body.reason,
        operation_name="revision.reject",
        key=idempotency_key,
    )


async def _revision_decision(
    session: StudySessionDep,
    *,
    runtime: StudyRuntime,
    auth: AuthContext,
    plan_id: UUID,
    revision_id: UUID,
    expected_version: int,
    decision: str,
    reason: str | None,
    operation_name: str,
    key: str,
) -> JSONResponse:
    replay = await _replay_or_none(
        session,
        user_id=auth.user_id,
        operation_name=operation_name,
        key=key,
        payload={
            "plan_id": str(plan_id),
            "revision_id": str(revision_id),
            "expected_version": expected_version,
            "reason": reason,
        },
        runtime=runtime,
    )
    if replay is not None:
        return replay
    revision = await plan_service.decide_revision(
        session,
        user_id=auth.user_id,
        plan_id=plan_id,
        revision_id=revision_id,
        expected_version=expected_version,
        decision=decision,
        reason=reason,
    )
    content = RevisionOut.model_validate(revision).model_dump(mode="json")
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


@router.post("/plans/{plan_id}/adjustments")
async def adjust_plan(
    plan_id: UUID,
    body: PlanActivateRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    """用户主动要求重新安排（§9.4/§12.2）：创建 replan operation。

    重大调整 → operation needs_input + proposed revision（等 accept/reject）；
    局部调整 → 自动激活。返回 202 + operation_id（前端轮询 operations）。
    """
    from uuid import uuid4

    from backend.study.persistence import repositories as repo
    from backend.study.services.replan import run_replan_operation

    plan = await repo.get_plan_row(session, user_id=auth.user_id, plan_id=plan_id)
    if plan is None:
        from backend.study.contracts.errors import StudyPlanNotFoundError

        raise StudyPlanNotFoundError("计划不存在或不属于当前用户")
    replay = await _replay_or_none(
        session,
        user_id=auth.user_id,
        operation_name="plan.adjustments",
        key=idempotency_key,
        payload={"plan_id": str(plan_id), "expected_version": body.expected_version},
        runtime=runtime,
    )
    if replay is not None:
        return replay
    if not await repo.bump_plan_version(
        session, plan_id=plan_id, expected_version=body.expected_version
    ):
        await _raise_version_conflict_plans(session, plan_id)
    operation_id = uuid4()
    await repo.insert_operation(
        session,
        operation_id=operation_id,
        user_id=auth.user_id,
        operation_type="replan",
        payload={
            "plan_id": str(plan_id),
            "reason": "user_adjustment",
            "user_requested": True,
        },
    )
    # 同步执行确定性 replan（无模型节点，与 Worker 路径同一函数）
    result = await run_replan_operation(
        session,
        operation={
            "operation_id": str(operation_id),
            "user_id": str(auth.user_id),
            "operation_type": "replan",
            "payload": {
                "plan_id": str(plan_id),
                "reason": "user_adjustment",
                "user_requested": True,
            },
        },
        settings=runtime.settings,
    )
    content = {
        "operation_id": str(operation_id),
        "status": result["status"],
        "revision_id": result.get("revision_id"),
    }
    await record_idempotent_result(
        session,
        user_id=auth.user_id,
        operation_name="plan.adjustments",
        idempotency_key=idempotency_key,
        response_status=202,
        response_body=content,
        operation_id=operation_id,
    )
    await session.commit()
    return JSONResponse(status_code=202, content=content)


async def _raise_version_conflict_plans(session: StudySessionDep, plan_id: UUID) -> None:
    from backend.study.contracts.errors import StudyPlanVersionConflictError

    current = await repo.get_plan_version(session, plan_id=plan_id)
    raise StudyPlanVersionConflictError("计划版本冲突，请刷新后重试", current_version=current)
