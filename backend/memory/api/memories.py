"""学习证据、用户记忆命令与总结记忆查询（规格 §19.1 / §19.2 / §19.4 / §12）。

- 写请求只接受公开字段；user_id/actor_type/priority/graph_thread_id 由
  Gateway 注入，客户端传入一律被 extra="forbid" 拒绝（422 REQUEST_EXTRA_FIELD）。
- P0 命令经 §14.2 快速路径：2 秒内完成 200，否则 202。
- 读接口所有列表使用 §19.9 HMAC 签名不透明 cursor。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import Field

from backend.auth.context import (
    SCOPE_MEMORY_CONTEXT,
    SCOPE_MEMORY_CORRECT,
    SCOPE_MEMORY_DELETE,
    SCOPE_MEMORY_READ,
    SCOPE_MEMORY_RESTORE,
    SCOPE_MEMORY_SUBMIT_EVIDENCE,
    AuthContext,
)
from backend.auth.verifier import AuthError
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
from backend.memory.contracts.commands import (
    CorrectMemoryCommand,
    ForgetMemoryCommand,
    OverrideLearnerProfileCommand,
    RestoreMemoryCommand,
)
from backend.memory.contracts.common import TopicKeyError, validate_existing_topic_key
from backend.memory.contracts.context import LearningContext, LearningContextRequest
from backend.memory.contracts.errors import InvalidPayloadError, MemoryNotFoundError
from backend.memory.contracts.evidence import ActivityEvidence, ConversationEvidence
from backend.memory.contracts.operations import MemoryOperationResult
from backend.memory.contracts.results import (
    CursorPage,
    DeletedMemoryItem,
    LearnerMemoryView,
    MasteryMemoryView,
    MemoryIndexEntryView,
    MemoryIndexView,
    MemorySearchHit,
    MemorySearchRequest,
)
from backend.memory.persistence import documents as docs_repo
from backend.memory.services.context_service import LearningContextService
from backend.memory.services.search_service import SearchService, normalize_search_query
from backend.memory.storage.markdown_schema import LearnerDocument, MasteryDocument
from backend.settings import Settings

router = APIRouter(prefix="/api/v1/memory", tags=["memory"])

_USER_ONLY = frozenset({"user"})
#: 证据只接受内部 Agent（裁决 2026-08-12：用户修改记忆走 command 通道，不提交证据）
_EVIDENCE_ACTORS = frozenset({"conversation_agent", "activity_agent"})
#: 检索/上下文：用户与带 delegated user 的内部 Agent（§18.3/§19.8）
_READ_AGENT_ACTORS = frozenset({"user", "conversation_agent", "activity_agent"})

#: POST /events 公开请求体：ConversationEvidence 或 ActivityEvidence 的公开字段（§19.1）
MemoryEventRequest = Annotated[
    ConversationEvidence | ActivityEvidence,
    Field(discriminator="kind"),
]


def _learner_view(doc: LearnerDocument) -> LearnerMemoryView:
    return LearnerMemoryView(
        version=doc.version,
        preferences=doc.preferences,
        goals=doc.goals,
        plans=doc.plans,
        evidence_refs=doc.evidence_refs,
        confidence=doc.confidence,
        updated_at=doc.updated_at,
    )


def _mastery_view(doc: MasteryDocument) -> MasteryMemoryView:
    return MasteryMemoryView(
        memory_id=f"mastery:{doc.topic_key}",
        topic_key=doc.topic_key,
        topic_title=doc.topic_title,
        version=doc.version,
        overview=doc.overview,
        understood=doc.understood,
        difficulties=doc.difficulties,
        review_advice=doc.review_advice,
        evidence_refs=doc.evidence_refs,
        confidence=doc.confidence,
        updated_at=doc.updated_at,
    )


def _check_evidence_permission(auth: AuthContext, payload: Any) -> None:
    """证据提交的 actor 边界（§18.3 权限矩阵）。

    裁决 2026-08-12：用户不提交证据（修改记忆走 command 通道），user actor
    由 _EVIDENCE_ACTORS 白名单在依赖层 403；此处只约束 Agent 各自的证据类型。
    """
    if auth.actor_type == "conversation_agent":
        if not isinstance(payload, ConversationEvidence):
            raise AuthError("AUTH_FORBIDDEN", "conversation_agent 只能提交对话证据", forbidden=True)
    elif auth.actor_type == "activity_agent":
        if not isinstance(payload, ActivityEvidence):
            raise AuthError("AUTH_FORBIDDEN", "activity_agent 只能提交行为证据", forbidden=True)


# ---------------------------------------------------------------------------
# 写接口（§19.1 / §19.2）
# ---------------------------------------------------------------------------


_EVIDENCE_REQUIRE = require(actors=_EVIDENCE_ACTORS, scope=SCOPE_MEMORY_SUBMIT_EVIDENCE)


@router.post("/events", response_model=MemoryOperationResult)
async def submit_event(
    request: MemoryEventRequest,
    response: Response,
    auth: AuthContext = Depends(_EVIDENCE_REQUIRE),
    runtime: ApiRuntime = Depends(get_runtime),
    trace_id: str = Depends(get_trace_id),
    idempotency_key: str = Depends(require_idempotency_key),
    _rate: None = Depends(rate_limit("write")),
) -> MemoryOperationResult:
    """学习证据入队（§19.1）；P2/P3 不走快速路径，固定 202。"""
    _check_evidence_permission(auth, request)
    row = await submit_operation(
        runtime,
        auth=auth,
        payload=request,
        public_hash_input=request.model_dump(mode="json"),
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )
    result = operation_result_from_row(row)
    response.status_code = status_code_for_row(row)
    return result


async def _submit_command(
    runtime: ApiRuntime,
    *,
    auth: AuthContext,
    command: Any,
    idempotency_key: str,
    trace_id: str,
    response: Response,
) -> MemoryOperationResult:
    """P0 用户命令公共路径（§19.2 / §14.2）。"""
    row = await submit_operation(
        runtime,
        auth=auth,
        payload=command,
        public_hash_input=command.model_dump(mode="json"),
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )
    result = operation_result_from_row(row)
    response.status_code = status_code_for_row(row)
    return result


@router.post("/commands/correct", response_model=MemoryOperationResult)
async def correct_memory(
    command: CorrectMemoryCommand,
    response: Response,
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_CORRECT)),
    runtime: ApiRuntime = Depends(get_runtime),
    trace_id: str = Depends(get_trace_id),
    idempotency_key: str = Depends(require_idempotency_key),
    _rate: None = Depends(rate_limit("write")),
) -> MemoryOperationResult:
    return await _submit_command(
        runtime,
        auth=auth,
        command=command,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        response=response,
    )


@router.post("/commands/forget", response_model=MemoryOperationResult)
async def forget_memory(
    command: ForgetMemoryCommand,
    response: Response,
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_DELETE)),
    runtime: ApiRuntime = Depends(get_runtime),
    trace_id: str = Depends(get_trace_id),
    idempotency_key: str = Depends(require_idempotency_key),
    _rate: None = Depends(rate_limit("write")),
) -> MemoryOperationResult:
    return await _submit_command(
        runtime,
        auth=auth,
        command=command,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        response=response,
    )


@router.post("/commands/restore", response_model=MemoryOperationResult)
async def restore_memory(
    command: RestoreMemoryCommand,
    response: Response,
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_RESTORE)),
    runtime: ApiRuntime = Depends(get_runtime),
    trace_id: str = Depends(get_trace_id),
    idempotency_key: str = Depends(require_idempotency_key),
    _rate: None = Depends(rate_limit("write")),
) -> MemoryOperationResult:
    return await _submit_command(
        runtime,
        auth=auth,
        command=command,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        response=response,
    )


@router.put("/learner", response_model=MemoryOperationResult)
async def override_learner(
    command: OverrideLearnerProfileCommand,
    response: Response,
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_CORRECT)),
    runtime: ApiRuntime = Depends(get_runtime),
    trace_id: str = Depends(get_trace_id),
    idempotency_key: str = Depends(require_idempotency_key),
    _rate: None = Depends(rate_limit("write")),
) -> MemoryOperationResult:
    return await _submit_command(
        runtime,
        auth=auth,
        command=command,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        response=response,
    )


# ---------------------------------------------------------------------------
# 读接口（§19.4）
# ---------------------------------------------------------------------------


@router.get("/learner", response_model=LearnerMemoryView)
async def get_learner(
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
) -> LearnerMemoryView:
    doc = await runtime.memory_service.get_learner(user_id=auth.user_id)
    if doc is None:
        raise MemoryNotFoundError("学习者档案不存在")
    return _learner_view(doc)


@router.get("/index", response_model=MemoryIndexView)
async def get_index(
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
) -> MemoryIndexView:
    """index 生命周期（§8.6.1）：未构建 version=0/stale=true，不 404。"""
    doc, stale = await runtime.memory_service.get_index(user_id=auth.user_id)
    if doc is None:
        return MemoryIndexView(version=0, entries=[], updated_at=None, stale=True)
    entries: list[MemoryIndexEntryView] = []
    if doc.learner is not None:
        entries.append(
            MemoryIndexEntryView(
                memory_id=doc.learner.memory_id,
                memory_type="learner",
                topic_key=doc.learner.topic_key,
                title=doc.learner.title,
                version=doc.learner.version,
                updated_at=doc.learner.updated_at,
            )
        )
    entries.extend(
        MemoryIndexEntryView(
            memory_id=entry.memory_id,
            memory_type="mastery",
            topic_key=entry.topic_key,
            title=entry.title,
            version=entry.version,
            updated_at=entry.updated_at,
        )
        for entry in doc.mastery_entries
    )
    return MemoryIndexView(
        version=doc.version, entries=entries, updated_at=doc.updated_at, stale=stale
    )


@router.get("/mastery/{topic_key}", response_model=MasteryMemoryView)
async def get_mastery(
    topic_key: str,
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
) -> MasteryMemoryView:
    try:
        validate_existing_topic_key(topic_key)
    except TopicKeyError as exc:
        raise InvalidPayloadError(str(exc), field="topic_key") from exc
    doc = await runtime.memory_service.get_mastery(user_id=auth.user_id, topic_key=topic_key)
    if doc is None:
        raise MemoryNotFoundError("掌握档案不存在")
    return _mastery_view(doc)


@router.get("/memories/{memory_id}", response_model=LearnerMemoryView | MasteryMemoryView)
async def get_memory_by_id(
    memory_id: str,
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
) -> LearnerMemoryView | MasteryMemoryView:
    if memory_id == "learner":
        learner = await runtime.memory_service.get_learner(user_id=auth.user_id)
        if learner is None:
            raise MemoryNotFoundError("记忆不存在")
        return _learner_view(learner)
    if memory_id.startswith("mastery:"):
        topic_key = memory_id.removeprefix("mastery:")
        try:
            validate_existing_topic_key(topic_key)
        except TopicKeyError as exc:
            raise InvalidPayloadError(str(exc), field="memory_id") from exc
        mastery = await runtime.memory_service.get_mastery(
            user_id=auth.user_id, topic_key=topic_key
        )
        if mastery is None:
            raise MemoryNotFoundError("记忆不存在")
        return _mastery_view(mastery)
    raise MemoryNotFoundError("记忆不存在")


@router.get("/deleted", response_model=CursorPage[DeletedMemoryItem])
async def list_deleted(
    cursor: str | None = Query(default=None, max_length=1000),
    limit: int = Query(default=20, ge=1, le=100),
    auth: AuthContext = Depends(require(actors=_USER_ONLY, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
    settings: Settings = Depends(get_settings),
) -> CursorPage[DeletedMemoryItem]:
    """30 天恢复窗口内的已删除记忆（§19.4）；cursor 绑定路由/用户/筛选。"""
    route = "memory.deleted"
    filters: dict[str, Any] = {"limit": limit}
    cursor_deleted_at: datetime | None = None
    cursor_memory_id: str | None = None
    if cursor is not None:
        payload = resolve_cursor(
            settings, cursor, route=route, user_id=auth.user_id, filters=filters
        )
        sort_key = payload.get("sort_key")
        if not isinstance(sort_key, list) or len(sort_key) != 2 or not isinstance(sort_key[0], str):
            raise InvalidPayloadError("cursor sort_key 非法", field="cursor")
        cursor_deleted_at = datetime.fromisoformat(sort_key[0])
        cursor_memory_id = str(sort_key[1])
    async with runtime.session_factory() as session:
        rows = await docs_repo.list_deleted_page(
            session,
            user_id=auth.user_id,
            now=datetime.now(UTC),
            limit=limit + 1,
            cursor_deleted_at=cursor_deleted_at,
            cursor_memory_id=cursor_memory_id,
        )
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [
        DeletedMemoryItem(
            memory_id=str(row["memory_id"]),
            memory_type=row["memory_type"],
            topic_key=row.get("topic_key"),
            title=str(row.get("topic_title") or row["memory_id"]),
            deleted_version=int(row["deleted_version"]),
            deleted_at=row["deleted_at"],
            restore_until=row["tombstone_until"],
        )
        for row in rows
    ]
    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = issue_cursor(
            settings,
            route=route,
            user_id=auth.user_id,
            filters=filters,
            sort_key=[last["deleted_at"].isoformat(), str(last["memory_id"])],
        )
    return CursorPage(items=items, next_cursor=next_cursor, has_more=has_more)


# ---------------------------------------------------------------------------
# 检索与上下文组装（§12 / §19.4；/context 形状见 contracts/context.py 注释）
# ---------------------------------------------------------------------------


@router.post("/search", response_model=CursorPage[MemorySearchHit])
async def search_memories(
    request: MemorySearchRequest,
    auth: AuthContext = Depends(require(actors=_READ_AGENT_ACTORS, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
    settings: Settings = Depends(get_settings),
    _rate: None = Depends(rate_limit("search")),
) -> CursorPage[MemorySearchHit]:
    """pg_trgm 混合检索（§12.1/§12.2）；限流 60/min（§18.5）；cursor 绑定筛选。"""
    route = "memory.search"
    filters: dict[str, Any] = {
        "query": normalize_search_query(request.query),
        "topic_keys": sorted(request.topic_keys),
        "memory_types": sorted(request.memory_types),
        "limit": request.limit,
    }
    cursor_sort_key: list[Any] | None = None
    if request.cursor is not None:
        payload = resolve_cursor(
            settings, request.cursor, route=route, user_id=auth.user_id, filters=filters
        )
        sort_key = payload.get("sort_key")
        if (
            not isinstance(sort_key, list)
            or len(sort_key) != 4
            or not isinstance(sort_key[0], int | float)
            or not isinstance(sort_key[1], str)
            or not isinstance(sort_key[2], str)
            or not isinstance(sort_key[3], int | float)
        ):
            raise InvalidPayloadError("cursor sort_key 非法", field="cursor")
        cursor_sort_key = sort_key
    service = SearchService(settings=settings, session_factory=runtime.session_factory)
    hits, next_sort_key, has_more = await service.search(
        user_id=auth.user_id, request=request, cursor_sort_key=cursor_sort_key
    )
    next_cursor: str | None = None
    if has_more and next_sort_key is not None:
        next_cursor = issue_cursor(
            settings,
            route=route,
            user_id=auth.user_id,
            filters=filters,
            sort_key=next_sort_key,
        )
    return CursorPage(items=hits, next_cursor=next_cursor, has_more=has_more)


@router.post("/context", response_model=LearningContext)
async def build_learning_context(
    request: LearningContextRequest,
    auth: AuthContext = Depends(require(actors=_READ_AGENT_ACTORS, scope=SCOPE_MEMORY_CONTEXT)),
    runtime: ApiRuntime = Depends(get_runtime),
    settings: Settings = Depends(get_settings),
) -> LearningContext:
    """学习上下文组装（§12.4/§12.5）；scope memory:context（§18.2）。"""
    service = LearningContextService(
        settings=settings,
        session_factory=runtime.session_factory,
        memory_service=runtime.memory_service,
    )
    return await service.build(user_id=auth.user_id, request=request)
