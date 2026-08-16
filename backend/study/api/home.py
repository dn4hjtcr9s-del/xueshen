"""Study 首页 API（§12.6/D9/D22：GET 无副作用，未来日期 422）。

- date 缺省由服务端按 active plan IANA 时区计算；调用方提交的日期不得晚于
  服务端判定的今天（§12.6，不信任浏览器时钟）；
- 无 active plan 时返回 no_active_plan 状态，不创建任何东西（D22 的 GET 侧）。
- POST /home/ensure-today 在 Phase 3 与 daily feed 一并实现。
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.exceptions import RequestValidationError

from backend.auth.context import AuthContext
from backend.shared.auth_context import get_auth_context
from backend.study.api.dependencies import StudyRuntimeDep, StudySessionDep
from backend.study.contracts.api import HomeOut
from backend.study.persistence import repositories as repo
from backend.study.services.home_service import aggregate_home, server_today

router = APIRouter()


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
    return HomeOut.model_validate(home)
