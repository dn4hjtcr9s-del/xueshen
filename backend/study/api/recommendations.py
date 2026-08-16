"""Study 推荐 API（§12.4/D13，v1.2）。

POST /recommendations/{feed_item_id}/accept：推荐加入今日正式任务（D13 规则）；
POST /recommendations/{feed_item_id}/dismiss：忽略/不再推荐。
两者都要求 Idempotency-Key（§15.1）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse

from backend.auth.context import AuthContext
from backend.shared.auth_context import get_auth_context
from backend.study.api.dependencies import StudyRuntime, StudyRuntimeDep, StudySessionDep
from backend.study.contracts.api import AcceptRecommendationOut
from backend.study.services import recommendation as rec_service
from backend.study.services.idempotency import (
    open_idempotent_request,
    record_idempotent_result,
)

router = APIRouter()


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


@router.post("/recommendations/{feed_item_id}/accept", response_model=AcceptRecommendationOut)
async def accept_recommendation(
    feed_item_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    replay = await _replay(
        session,
        runtime=runtime,
        user_id=auth.user_id,
        operation_name="recommendation.accept",
        key=idempotency_key,
        payload={"feed_item_id": str(feed_item_id)},
    )
    if replay is not None:
        return replay
    result = await rec_service.accept_recommendation(
        session, user_id=auth.user_id, feed_item_id=feed_item_id, now=datetime.now().astimezone()
    )
    content = AcceptRecommendationOut.model_validate(result).model_dump(mode="json")
    await record_idempotent_result(
        session,
        user_id=auth.user_id,
        operation_name="recommendation.accept",
        idempotency_key=idempotency_key,
        response_status=200,
        response_body=content,
    )
    await session.commit()
    return JSONResponse(status_code=200, content=content)


@router.post("/recommendations/{feed_item_id}/dismiss")
async def dismiss_recommendation(
    feed_item_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
    runtime: StudyRuntimeDep,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> JSONResponse:
    replay = await _replay(
        session,
        runtime=runtime,
        user_id=auth.user_id,
        operation_name="recommendation.dismiss",
        key=idempotency_key,
        payload={"feed_item_id": str(feed_item_id)},
    )
    if replay is not None:
        return replay
    await rec_service.dismiss_recommendation(
        session, user_id=auth.user_id, feed_item_id=feed_item_id
    )
    content = {"feed_item_id": str(feed_item_id), "status": "dismissed"}
    await record_idempotent_result(
        session,
        user_id=auth.user_id,
        operation_name="recommendation.dismiss",
        idempotency_key=idempotency_key,
        response_status=200,
        response_body=content,
    )
    await session.commit()
    return JSONResponse(status_code=200, content=content)
