"""Study Operation API（§12.7）。

needs_input = 重大调整已生成 proposed revision 等待决策（§9.4/D21）；
cancelled 包括用户 reject proposed revision 或显式取消。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends

from backend.auth.context import AuthContext
from backend.shared.auth_context import get_auth_context
from backend.study.api.dependencies import StudySessionDep
from backend.study.contracts.api import OperationOut
from backend.study.contracts.errors import StudyOperationNotFoundError
from backend.study.persistence import repositories as repo

router = APIRouter()


@router.get("/operations/{operation_id}", response_model=OperationOut)
async def get_operation(
    operation_id: UUID,
    auth: Annotated[AuthContext, Depends(get_auth_context)],
    session: StudySessionDep,
) -> OperationOut:
    row = await repo.get_operation_row(session, user_id=auth.user_id, operation_id=operation_id)
    if row is None:
        raise StudyOperationNotFoundError("operation 不存在或不属于当前用户")
    return OperationOut.model_validate(row)
