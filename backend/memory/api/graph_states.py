"""知识图谱读取与 Overlay 状态写接口（规格 §19.5 / §6.4 / §14.2）。

- 固定图谱只读；用户 Overlay 写入统一返回 MemoryOperationResult（P1 快速路径）。
- PUT body 只接受 action/expected_version；任何 expert 设置企图 422
  GRAPH_STATUS_NOT_USER_SETTABLE；额外字段 422 REQUEST_EXTRA_FIELD。
- DELETE 以 query 参数承载 expected_version；有 Overlay 缺版本 422
  GRAPH_STATE_VERSION_REQUIRED；无 Overlay 幂等 no_change。
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Path, Query, Response
from pydantic import BaseModel, ConfigDict, Field

from backend.auth.context import (
    SCOPE_MEMORY_GRAPH_STATE,
    SCOPE_MEMORY_READ,
    AuthContext,
)
from backend.memory.api.dependencies import (
    ApiRuntime,
    get_runtime,
    get_trace_id,
    operation_result_from_row,
    rate_limit,
    require,
    require_idempotency_key,
    status_code_for_row,
    submit_operation,
)
from backend.memory.contracts.commands import GraphStateCommand
from backend.memory.contracts.errors import (
    GraphNodeNotFoundError,
    GraphStateVersionRequiredError,
    GraphStatusNotUserSettableError,
    InvalidPayloadError,
)
from backend.memory.contracts.graph_state import (
    GraphEdgeView,
    GraphNodeDetailView,
    GraphNodeView,
    GraphOverlayView,
    GraphStateExplanation,
    KnowledgeGraphSnapshot,
)
from backend.memory.contracts.operations import MemoryOperationResult
from backend.memory.knowledge_graph.registry import KnowledgeGraphRegistry
from backend.memory.persistence import graph_states as graph_repo

router = APIRouter(prefix="/api/v1/knowledge-graph", tags=["knowledge-graph"])

_READ_ACTORS = frozenset({"user", "knowledge_graph_ui"})
_WRITE_ACTORS = frozenset({"user", "knowledge_graph_ui"})

#: 用户可设置的图谱动作（§16.2）；expert 永远不可手动设置
_USER_ACTIONS = frozenset({"mark_unfamiliar", "mark_familiar"})

#: graph_state_audit.actor_type → 公开 source_type（§19.5）
_SOURCE_TYPE_MAP = {
    "user": "user",
    "summary_projection": "summary_memory",
    "summary_memory": "summary_memory",
    "system": "system_recompute",
    "system_recompute": "system_recompute",
}


class _GraphStatePutBody(BaseModel):
    """PUT state 原始 body：action 先按字符串接收，以便把 expert 映射到专用错误码。

    字段集合与 GraphStatePutRequest 一致（extra="forbid"，§6.4）。
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(min_length=1, max_length=40)
    expected_version: int | None = Field(default=None, ge=1)


def _node_view(row: dict[str, Any]) -> GraphNodeView:
    return GraphNodeView(
        node_id=str(row["node_id"]),
        title=str(row["title"]),
        group_key=row.get("group_key"),
        metadata=dict(row.get("metadata") or {}),
    )


def _overlay_view(row: dict[str, Any]) -> GraphOverlayView:
    return GraphOverlayView(
        node_id=str(row["node_id"]),
        status=row.get("status"),
        version=row.get("version"),
        status_source=row.get("status_source"),
        updated_at=row.get("updated_at"),
    )


# ---------------------------------------------------------------------------
# 读接口（§19.5）
# ---------------------------------------------------------------------------


@router.get("/nodes", response_model=KnowledgeGraphSnapshot)
async def get_knowledge_graph(
    auth: AuthContext = Depends(require(actors=_READ_ACTORS, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
) -> KnowledgeGraphSnapshot:
    """全量固定节点、分组和边，不分页、不返回用户状态（§19.5）。"""
    async with runtime.session_factory() as session:
        registry = KnowledgeGraphRegistry(session)
        nodes = await registry.list_nodes()
        edges = await registry.list_edges()
        sync = await registry.latest_applied_sync()
    if sync is None:
        # 注册表未同步：readiness 已不通过；这里返回空快照（零 checksum 占位）
        from datetime import UTC, datetime

        return KnowledgeGraphSnapshot(
            nodes=[], edges=[], manifest_checksum="0" * 64, synced_at=datetime.fromtimestamp(0, UTC)
        )
    return KnowledgeGraphSnapshot(
        nodes=[_node_view(row) for row in nodes],
        edges=[
            GraphEdgeView(
                from_node_id=str(row["from_node_id"]),
                to_node_id=str(row["to_node_id"]),
                relation_type="prerequisite",
            )
            for row in edges
        ],
        manifest_checksum=str(sync["manifest_checksum"]),
        synced_at=sync["applied_at"],
    )


@router.get("/me/nodes", response_model=list[GraphOverlayView])
async def list_my_node_states(
    auth: AuthContext = Depends(require(actors=_READ_ACTORS, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
) -> list[GraphOverlayView]:
    """当前用户全部 Overlay；无行节点由前端解释为无状态（§19.5）。"""
    async with runtime.session_factory() as session:
        rows = await graph_repo.list_overlays(session, user_id=auth.user_id)
    return [_overlay_view(row) for row in rows]


@router.get("/me/nodes/{node_id}", response_model=GraphNodeDetailView)
async def get_my_node_detail(
    node_id: str = Path(pattern=r"^n\d{3,}$"),
    auth: AuthContext = Depends(require(actors=_READ_ACTORS, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
) -> GraphNodeDetailView:
    async with runtime.session_factory() as session:
        registry = KnowledgeGraphRegistry(session)
        node = await registry.get_node(node_id)
        if node is None:
            raise GraphNodeNotFoundError("图谱节点不存在")
        prerequisites = await registry.edges_to(node_id)
        successors = await registry.edges_from(node_id)
        overlay = await graph_repo.get_overlay(session, user_id=auth.user_id, node_id=node_id)
    return GraphNodeDetailView(
        node=_node_view(node),
        overlay=(
            _overlay_view(overlay)
            if overlay is not None
            else GraphOverlayView(
                node_id=node_id, status=None, version=None, status_source=None, updated_at=None
            )
        ),
        prerequisite_node_ids=prerequisites,
        successor_node_ids=successors,
    )


@router.get("/me/nodes/{node_id}/explanation", response_model=GraphStateExplanation)
async def get_node_explanation(
    node_id: str = Path(pattern=r"^n\d{3,}$"),
    auth: AuthContext = Depends(require(actors=_READ_ACTORS, scope=SCOPE_MEMORY_READ)),
    runtime: ApiRuntime = Depends(get_runtime),
) -> GraphStateExplanation:
    """由最近一次 graph_state_audit 生成受控解释（§19.5）。"""
    async with runtime.session_factory() as session:
        registry = KnowledgeGraphRegistry(session)
        if not await registry.node_exists(node_id):
            raise GraphNodeNotFoundError("图谱节点不存在")
        overlay = await graph_repo.get_overlay(session, user_id=auth.user_id, node_id=node_id)
        audit = await graph_repo.latest_audit(session, user_id=auth.user_id, node_id=node_id)
    summary = audit.get("explanation_summary") if audit else None
    return GraphStateExplanation(
        node_id=node_id,
        current_status=overlay.get("status") if overlay else None,
        explanation_available=summary is not None,
        summary=summary,
        reason_codes=list(audit.get("reason_codes") or []) if audit else [],
        source_type=(
            _SOURCE_TYPE_MAP.get(str(audit["actor_type"])) if audit else None  # type: ignore[arg-type]
        ),
        source_memory_id=overlay.get("source_memory_id") if overlay else None,
        source_memory_version=overlay.get("source_memory_version") if overlay else None,
        evidence_refs=list(audit.get("evidence_refs") or [])[:10] if audit else [],
        changed_at=audit.get("created_at") if audit else None,
    )


# ---------------------------------------------------------------------------
# 写接口（§19.5 / §6.4）：统一返回 MemoryOperationResult，P1 快速路径
# ---------------------------------------------------------------------------


async def _submit_graph_state(
    runtime: ApiRuntime,
    *,
    auth: AuthContext,
    node_id: str,
    action: Literal["mark_unfamiliar", "mark_familiar", "clear"],
    expected_version: int | None,
    idempotency_key: str,
    trace_id: str,
    response: Response,
) -> MemoryOperationResult:
    async with runtime.session_factory() as session:
        registry = KnowledgeGraphRegistry(session)
        if not await registry.node_exists(node_id):
            raise GraphNodeNotFoundError("图谱节点不存在")
    command = GraphStateCommand(node_id=node_id, action=action, expected_version=expected_version)
    row = await submit_operation(
        runtime,
        auth=auth,
        payload=command,
        public_hash_input={
            "path": {"node_id": node_id},
            "body": {"action": action, "expected_version": expected_version},
        },
        idempotency_key=idempotency_key,
        trace_id=trace_id,
    )
    result = operation_result_from_row(row)
    response.status_code = status_code_for_row(row)
    return result


@router.put("/me/nodes/{node_id}/state", response_model=MemoryOperationResult)
async def put_node_state(
    body: _GraphStatePutBody,
    response: Response,
    node_id: str = Path(pattern=r"^n\d{3,}$"),
    auth: AuthContext = Depends(require(actors=_WRITE_ACTORS, scope=SCOPE_MEMORY_GRAPH_STATE)),
    runtime: ApiRuntime = Depends(get_runtime),
    trace_id: str = Depends(get_trace_id),
    idempotency_key: str = Depends(require_idempotency_key),
    _rate: None = Depends(rate_limit("graph_state")),
) -> MemoryOperationResult:
    if body.action == "expert":
        raise GraphStatusNotUserSettableError(
            "精通状态由长期学习表现自动评估，不能手动设置。", field="action"
        )
    if body.action not in _USER_ACTIONS:
        raise InvalidPayloadError(f"非法 action: {body.action}", field="action")
    return await _submit_graph_state(
        runtime,
        auth=auth,
        node_id=node_id,
        action=body.action,  # type: ignore[arg-type]
        expected_version=body.expected_version,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        response=response,
    )


@router.delete("/me/nodes/{node_id}/state", response_model=MemoryOperationResult)
async def delete_node_state(
    response: Response,
    node_id: str = Path(pattern=r"^n\d{3,}$"),
    expected_version: int | None = Query(default=None, ge=1),
    auth: AuthContext = Depends(require(actors=_WRITE_ACTORS, scope=SCOPE_MEMORY_GRAPH_STATE)),
    runtime: ApiRuntime = Depends(get_runtime),
    trace_id: str = Depends(get_trace_id),
    idempotency_key: str = Depends(require_idempotency_key),
    _rate: None = Depends(rate_limit("graph_state")),
) -> MemoryOperationResult:
    """clear：有 Overlay 缺版本 422；无 Overlay 可省略版本并幂等 no_change（§19.5）。"""
    async with runtime.session_factory() as session:
        overlay = await graph_repo.get_overlay(session, user_id=auth.user_id, node_id=node_id)
    if overlay is not None and expected_version is None:
        raise GraphStateVersionRequiredError(
            "当前存在图谱状态，clear 必须携带 expected_version", field="expected_version"
        )
    return await _submit_graph_state(
        runtime,
        auth=auth,
        node_id=node_id,
        action="clear",
        expected_version=expected_version,
        idempotency_key=idempotency_key,
        trace_id=trace_id,
        response=response,
    )
