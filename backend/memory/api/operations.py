"""Operation 查询与取消（规格 §19.3 / §11.6）。

只能访问当前用户 operation；terminal 状态不允许再次取消（409）。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response

from backend.auth.context import (
    SCOPE_MEMORY_CANCEL,
    SCOPE_MEMORY_GRAPH_STATE,
    SCOPE_MEMORY_READ,
    AuthContext,
)
from backend.memory.api.dependencies import (
    ApiRuntime,
    get_runtime,
    operation_result_from_row,
    rate_limit,
    require,
    require_idempotency_key,
    status_code_for_row,
)
from backend.memory.contracts.errors import (
    OperationCancelNotAllowedError,
    OperationNotFoundError,
)
from backend.memory.contracts.operations import MemoryOperationResult
from backend.memory.persistence import operations as ops_repo

router = APIRouter(prefix="/api/v1/memory/operations", tags=["memory-operations"])

#: knowledge_graph_ui 需要轮询自己发起的图谱标记 operation（§20.3）
_OPERATION_READ_ACTORS = frozenset({"user", "knowledge_graph_ui"})


@router.get("/{operation_id}", response_model=MemoryOperationResult)
async def get_operation(
    response: Response,
    operation_id: UUID,
    auth: AuthContext = Depends(
        require(
            actors=_OPERATION_READ_ACTORS,
            any_scopes=(SCOPE_MEMORY_READ, SCOPE_MEMORY_GRAPH_STATE),
        )
    ),
    runtime: ApiRuntime = Depends(get_runtime),
) -> MemoryOperationResult:
    """查询 operation 状态（§19.3）；他人 operation 一律 404（§18.4 IDOR）。"""
    async with runtime.session_factory() as session:
        row = await ops_repo.list_user_operations(
            session, user_id=auth.user_id, operation_id=operation_id
        )
    if row is None:
        raise OperationNotFoundError("operation 不存在")
    result = operation_result_from_row(row)
    response.status_code = status_code_for_row(row)
    return result


@router.post("/{operation_id}/cancel", response_model=MemoryOperationResult)
async def cancel_operation(
    response: Response,
    operation_id: UUID,
    auth: AuthContext = Depends(require(actors=_OPERATION_READ_ACTORS, scope=SCOPE_MEMORY_CANCEL)),
    runtime: ApiRuntime = Depends(get_runtime),
    _idempotency_key: str = Depends(require_idempotency_key),
    _rate: None = Depends(rate_limit("write")),
) -> MemoryOperationResult:
    """取消规则（§11.6）：queued/retry_wait 立即取消；running 协作取消；

    needs_review 可取消；succeeded/dead_letter/cancelled 返回 409。
    """
    async with runtime.session_factory() as session:
        async with session.begin():
            row = await ops_repo.list_user_operations(
                session, user_id=auth.user_id, operation_id=operation_id
            )
            if row is None:
                raise OperationNotFoundError("operation 不存在")
            updated = await ops_repo.request_cancel(session, operation_id=operation_id)
            if updated is None:
                raise OperationCancelNotAllowedError(
                    "operation 当前状态不允许取消（已终态或已进入 commit）"
                )
    result = operation_result_from_row(updated)
    response.status_code = status_code_for_row(updated)
    return result
