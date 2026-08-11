"""候选审核接口（规格 §19.2 / §19.4 / §6.3）。

- 列表使用 §19.9 签名 cursor，绑定 status/limit 筛选。
- decision 的 candidate_id 由 URL 路径注入（ReviewDecisionRequest 同
  GraphStatePutRequest 模式）；他人候选一律 404（§18.4）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from backend.auth.context import SCOPE_MEMORY_READ, SCOPE_MEMORY_REVIEW, AuthContext
from backend.memory.api.dependencies import (
    ApiRuntime,
    get_runtime,
    get_settings,
    get_trace_id,
    issue_cursor,
    operation_result_from_row,
    rate_limit,
    require,
    require_idempotency_key,
    resolve_cursor,
    status_code_for_row,
    submit_operation,
)
from backend.memory.contracts.commands import CandidateContentView, ReviewDecisionRequest
from backend.memory.contracts.errors import (
    CandidateAlreadyReviewedError,
    CandidateNotFoundError,
    InvalidPayloadError,
)
from backend.memory.contracts.operations import MemoryOperationResult
from backend.memory.contracts.results import CursorPage, ReviewCandidateView
from backend.memory.persistence import review_candidates as candidates_repo
from backend.settings import Settings

router = APIRouter(prefix="/api/v1/memory/review-candidates", tags=["memory-review-candidates"])

_USER_ONLY = frozenset({"user"})

_CandidateStatus = Literal["pending", "accepted", "corrected", "rejected", "expired"]


def _candidate_view(row: dict[str, Any]) -> ReviewCandidateView:
    return ReviewCandidateView(
        candidate_id=UUID(str(row["candidate_id"])),
        candidate_type=row["candidate_type"],
        base_memory_id=row.get("base_memory_id"),
        base_version=row.get("base_version"),
        topic_key=row.get("topic_key"),
        candidate_content=CandidateContentView.model_validate(row["candidate_payload"]),
        evidence_refs=list(row.get("evidence_refs") or []),
        confidence=float(row["confidence"]),
        status=row["status"],
        resolution_target=row.get("resolution_target"),
        target_memory_id=row.get("target_memory_id"),
        resolved_operation_id=(
            UUID(str(row["resolved_operation_id"])) if row.get("resolved_operation_id") else None
        ),
        reviewed_at=row.get("reviewed_at"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@router.get("", response_model=CursorPage[ReviewCandidateView])
async def list_review_candidates(
    status: _CandidateStatus | None = Query(default=None),
    cursor: str | None = Query(default=None, max_length=1000),
    limit: int = Query(default=20, ge=1, le=100),
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
    settings: Settings = Depends(get_settings),
) -> CursorPage[ReviewCandidateView]:
    route = "memory.review_candidates"
    filters: dict[str, Any] = {"status": status, "limit": limit}
    cursor_created_at: datetime | None = None
    cursor_candidate_id: UUID | None = None
    if cursor is not None:
        payload = resolve_cursor(
            settings, cursor, route=route, user_id=auth.user_id, filters=filters
        )
        sort_key = payload.get("sort_key")
        if not isinstance(sort_key, list) or len(sort_key) != 2 or not isinstance(sort_key[0], str):
            raise InvalidPayloadError("cursor sort_key 非法", field="cursor")
        cursor_created_at = datetime.fromisoformat(sort_key[0])
        cursor_candidate_id = UUID(str(sort_key[1]))
    async with runtime.session_factory() as session:
        rows = await candidates_repo.list_candidates_page(
            session,
            user_id=auth.user_id,
            status=status,
            limit=limit + 1,
            cursor_created_at=cursor_created_at,
            cursor_candidate_id=cursor_candidate_id,
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = issue_cursor(
            settings,
            route=route,
            user_id=auth.user_id,
            filters=filters,
            sort_key=[last["created_at"].isoformat(), str(last["candidate_id"])],
        )
    return CursorPage(
        items=[_candidate_view(row) for row in rows],
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.post("/{candidate_id}/decision", response_model=MemoryOperationResult)
async def decide_review_candidate(
    candidate_id: UUID,
    request: ReviewDecisionRequest,
    response: Response,
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_REVIEW)),
    runtime: ApiRuntime = Depends(get_runtime),
    trace_id: str = Depends(get_trace_id),
    idempotency_key: str = Depends(require_idempotency_key),
    _rate: None = Depends(rate_limit("write")),
) -> MemoryOperationResult:
    """候选审核 P0 命令（§19.2）；候选归属与非 pending 状态在 Gateway 预检。"""
    async with runtime.session_factory() as session:
        candidate = await candidates_repo.get_candidate(session, candidate_id=candidate_id)
    if candidate is None or str(candidate["user_id"]) != str(auth.user_id):
        raise CandidateNotFoundError("候选不存在")
    if candidate["status"] != "pending":
        raise CandidateAlreadyReviewedError("候选已审核")
    command = request.to_command(candidate_id=candidate_id)
    row = await submit_operation(
        runtime,
        auth=auth,
        payload=command,
        public_hash_input={
            "path": {"candidate_id": str(candidate_id)},
            "body": request.model_dump(mode="json"),
        },
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )
    result = operation_result_from_row(row)
    response.status_code = status_code_for_row(row)
    return result
