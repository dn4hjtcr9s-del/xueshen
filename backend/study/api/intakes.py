"""Study Intake API（§12.1/D10，v1.2）。

- POST /intakes：新建 24 小时 TTL 的录入会话；
- POST /intakes/{id}/messages：同步执行单轮抽取与追问（200），不返回 operation；
- GET /intakes/{id}：读取当前状态与 intent；
- POST /intakes/{id}/confirm：ready → confirmed + 202 plan_generation operation。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from backend.auth.context import AuthContext
from backend.shared.auth_context import get_auth_context
from backend.study.api.dependencies import StudyRuntime, StudyRuntimeDep, StudySessionDep
from backend.study.contracts.api import IntakeOut
from backend.study.graph import intake_runner
from backend.study.services.idempotency import (
    open_idempotent_request,
    record_idempotent_result,
)

router = APIRouter()


class IntakeMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=2000)


def _gateway_for(runtime: StudyRuntime) -> Any:
    """按请求构建 OpenAIGateway（同步追问路径）；未配置 → 503 可重试。"""
    from backend.study.contracts.errors import StudyPlanGenerationFailedError
    from backend.study.gateways.openai import StudyOpenAIGateway

    try:
        return StudyOpenAIGateway(settings=runtime.settings)
    except ValueError as exc:
        raise StudyPlanGenerationFailedError(f"Intake 模型未配置: {exc}") from exc


def _intake_out(row: dict[str, Any]) -> IntakeOut:
    return IntakeOut.model_validate(row)


@router.post("/intakes", status_code=201, response_model=IntakeOut)
async def create_intake(
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
) -> IntakeOut:
    row = await intake_runner.create_intake(
        session, user_id=auth.user_id, settings=runtime.settings
    )
    return _intake_out(row)


@router.get("/intakes/{intake_id}", response_model=IntakeOut)
async def get_intake(
    intake_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
) -> IntakeOut:
    from sqlalchemy import text

    result = await session.execute(
        text(
            "SELECT * FROM study_plan_intakes WHERE intake_id = :intake_id AND user_id = :user_id"
        ),
        {"intake_id": intake_id, "user_id": auth.user_id},
    )
    row = result.mappings().first()
    if row is None:
        from backend.study.contracts.errors import StudyIntakeNotFoundError

        raise StudyIntakeNotFoundError("intake 不存在或不属于当前用户")
    return _intake_out(dict(row))


@router.post("/intakes/{intake_id}/messages")
async def post_intake_message(
    intake_id: UUID,
    body: IntakeMessageRequest,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    """同步执行单轮抽取（D10）：200 + 状态/追问/缺失字段；幂等重放返回首次结果。"""
    from sqlalchemy import text

    opened = await open_idempotent_request(
        session,
        user_id=auth.user_id,
        operation_name="intake.message",
        idempotency_key=idempotency_key,
        payload={"intake_id": str(intake_id), "message": body.message},
        now=datetime.now(UTC),
        retention_days=runtime.settings.study_idempotency_retention_days,
    )
    if opened.replay and opened.response_status is not None:
        return JSONResponse(status_code=opened.response_status, content=opened.response_body or {})
    result = await session.execute(
        text(
            "SELECT * FROM study_plan_intakes WHERE intake_id = :intake_id AND user_id = :user_id"
        ),
        {"intake_id": intake_id, "user_id": auth.user_id},
    )
    row = result.mappings().first()
    if row is None:
        from backend.study.contracts.errors import StudyIntakeNotFoundError

        raise StudyIntakeNotFoundError("intake 不存在或不属于当前用户")
    gateway = _gateway_for(runtime)
    refreshed, reply, status = await intake_runner.run_intake_turn(
        session=session,
        settings=runtime.settings,
        intake_row=dict(row),
        message=body.message,
        gateway=gateway,
    )
    content = {
        "intake": _intake_out(refreshed).model_dump(mode="json"),
        "reply": reply,
        "status": status,
    }
    await record_idempotent_result(
        session,
        user_id=auth.user_id,
        operation_name="intake.message",
        idempotency_key=idempotency_key,
        response_status=200,
        response_body=content,
    )
    await session.commit()
    return JSONResponse(status_code=200, content=content)


@router.post("/intakes/{intake_id}/confirm")
async def confirm_intake(
    intake_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
) -> JSONResponse:
    """confirm（§12.1）：ready → 202 operation_id；按 intake 自身幂等。"""
    from sqlalchemy import text

    result = await session.execute(
        text(
            "SELECT * FROM study_plan_intakes WHERE intake_id = :intake_id AND user_id = :user_id"
        ),
        {"intake_id": intake_id, "user_id": auth.user_id},
    )
    row = result.mappings().first()
    if row is None:
        from backend.study.contracts.errors import StudyIntakeNotFoundError

        raise StudyIntakeNotFoundError("intake 不存在或不属于当前用户")
    operation_id = await intake_runner.confirm_intake(
        session, intake_row=dict(row), user_id=auth.user_id
    )
    return JSONResponse(
        status_code=202, content={"operation_id": str(operation_id), "status": "queued"}
    )
