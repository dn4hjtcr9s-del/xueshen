"""父图节点与路由（§10.3）。

normalize_input → authorize_actor → idempotency_guard → validate_invariants
→ route_operation → 分支 → normalize_result
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from backend.memory.contracts.errors import InvalidPayloadError
from backend.memory.contracts.evidence import ActivityEvidence
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.graph.activity_exposure import EXPOSURE_ACTIVITY_TYPES
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext
from backend.memory.persistence.operations import get_operation

# actor_type × input_kind 确定性兼容矩阵（§18.2；scope 级授权在 Gateway 层）
ACTOR_INPUT_MATRIX: dict[str, set[str]] = {
    "user": {"command"},
    "admin": {"command", "maintenance"},
    "conversation_agent": {"evidence"},
    "activity_agent": {"evidence"},
    "knowledge_graph_ui": {"command"},
    "summary_projection": {"projection"},
    "system": {"evidence", "command", "projection", "maintenance"},
}


def _operation(state: MemoryManagerState) -> MemoryOperation:
    return MemoryOperation.model_validate(state["operation"])


async def normalize_input(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """标准化 operation：契约再校验，拒绝 schema_version 漂移。"""
    operation = _operation(state)
    if operation.schema_version != 1:
        raise InvalidPayloadError(f"不支持的 schema_version: {operation.schema_version}")
    if operation.payload.kind != operation.operation_type:
        raise InvalidPayloadError(
            f"payload kind {operation.payload.kind} 与 operation_type "
            f"{operation.operation_type} 不一致"
        )
    return {"warnings": [], "errors": [], "llm_call_count": 0, "replan_count": 0}


async def authorize_actor(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """actor × input_kind 兼容校验（§18.2）；用户注入 actor_type 由 Gateway 拒绝。"""
    operation = _operation(state)
    allowed = ACTOR_INPUT_MATRIX.get(operation.actor_type, set())
    if operation.input_kind not in allowed:
        raise InvalidPayloadError(
            f"actor_type {operation.actor_type} 不允许 {operation.input_kind} 操作"
        )
    return {}


async def idempotency_guard(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """只读检查：operation 已完成且有结果时直接复用（§11.3）。"""
    ctx = runtime.context
    operation = _operation(state)
    async with ctx.session_factory() as session:
        row = await get_operation(session, operation_id=operation.operation_id)
    if row and row["status"] == "succeeded" and row.get("result"):
        return {"route": "finalize_replay", "commit_result": dict(row["result"])}
    return {}


async def validate_invariants(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """只读不变量：evidence payload 必须携带来源引用；命令必须携带并发令牌。"""
    operation = _operation(state)
    payload = operation.payload
    from backend.memory.contracts.evidence import ActivityEvidence, ConversationEvidence

    if isinstance(payload, ConversationEvidence) and not payload.message_ids:
        raise InvalidPayloadError("conversation_evidence 缺少 message_ids")
    if isinstance(payload, ActivityEvidence) and not payload.activity_ids:
        raise InvalidPayloadError("activity_evidence 缺少 activity_ids")
    return {}


async def route_operation(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """确定性路由（§10.3 / §10.3.1 分流）。"""
    if state.get("route") == "finalize_replay":
        return {}
    operation = _operation(state)
    payload = operation.payload
    route_by_type = {
        "conversation_evidence": "summary",
        "correct_memory": "memory_command",
        "forget_memory": "memory_command",
        "restore_memory": "memory_command",
        "override_learner_profile": "memory_command",
        "review_candidate": "memory_command",
        "set_graph_state": "graph_state",
        "project_summary_to_graph": "projection",
        "rebuild_index": "maintenance",
        "verify_checksums": "maintenance",
        "purge_tombstones": "maintenance",
        "cleanup_orphan_versions": "maintenance",
        "cleanup_checkpoints": "maintenance",
        "purge_account_memory": "maintenance",
    }
    route: str | None
    if operation.operation_type == "activity_evidence":
        assert isinstance(payload, ActivityEvidence)
        route = (
            "activity_exposure" if payload.activity_type in EXPOSURE_ACTIVITY_TYPES else "summary"
        )
    else:
        route = route_by_type.get(operation.operation_type)
    if route is None:
        raise InvalidPayloadError(f"未路由的 operation_type: {operation.operation_type}")
    return {"route": route}


async def normalize_result(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """各分支结果 → 稳定公开结果（§5.4 MemoryOperationResult 同构）。"""
    operation = _operation(state)
    commit = state.get("commit_result", {})
    graph = state.get("graph_state_result", {})
    errors = state.get("errors", [])

    mutations = commit.get("mutations", [])
    review_ids = commit.get("review_candidate_ids", [])
    changes = graph.get("changes", [])

    budget_exhausted = any(e.get("code") == "LLM_BUDGET_EXHAUSTED" for e in errors)
    if budget_exhausted and not mutations and not review_ids:
        status = "dead_letter"
    elif review_ids and not mutations:
        status = "needs_review"
    else:
        status = "succeeded"

    result = {
        "operation_id": str(operation.operation_id),
        "status": status,
        "operation_type": operation.operation_type,
        "mutations": mutations,
        "review_candidate_ids": review_ids,
        "graph_state_changes": changes,
        "warnings": state.get("warnings", []),
        "graph_extras": {k: v for k, v in graph.items() if k != "changes"},
        "replayed": commit.get("replayed", False),
    }
    return {"commit_result": result}
