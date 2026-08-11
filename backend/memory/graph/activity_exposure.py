"""Activity exposure 分支（§10.3.1）。

page_view/bookmark/check_in：只更新 graph_user_node_activity 时间与计数，
不调用 OpenAI、不创建总结记忆、不更新 Overlay status，返回 no_change。
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from backend.memory.contracts.evidence import ActivityEvidence
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext
from backend.memory.persistence import graph_states as gs_repo

EXPOSURE_ACTIVITY_TYPES = ("page_view", "bookmark", "check_in")


async def validate_activity_hints(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """仅接受存在于只读注册表中的可靠 graph_node_hints；无可靠 hint 不猜节点。"""
    ctx = runtime.context
    operation = MemoryOperation.model_validate(state["operation"])
    payload = operation.payload
    assert isinstance(payload, ActivityEvidence)
    async with ctx.session_factory() as session:
        registry = ctx.graph_registry_factory(session)
        valid_nodes = [
            node_id
            for node_id in dict.fromkeys(payload.graph_node_hints)
            if await registry.node_exists(node_id)
        ]
    warnings = list(state.get("warnings", []))
    if payload.graph_node_hints and not valid_nodes:
        warnings.append("graph_node_hints 全部无效，按 no_change 处理")
    return {
        "candidate_graph_nodes": {"activity": [{"node_id": n} for n in valid_nodes]},
        "warnings": warnings,
    }


async def upsert_graph_node_activity(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """对每个去重后的 (user_id, node_id, activity_type, activity_id) 幂等计数（裁决 A）。"""
    ctx = runtime.context
    operation = MemoryOperation.model_validate(state["operation"])
    payload = operation.payload
    assert isinstance(payload, ActivityEvidence)
    nodes = [n["node_id"] for n in state.get("candidate_graph_nodes", {}).get("activity", [])]
    if not nodes:
        return {}
    occurred_at = payload.window_ended_at or ctx.clock.now()
    recorded = 0
    async with ctx.session_factory() as session:
        async with session.begin():
            for node_id in nodes:
                for activity_id in dict.fromkeys(payload.activity_ids):
                    if await gs_repo.record_activity_event_once(
                        session,
                        user_id=operation.user_id,
                        node_id=node_id,
                        activity_type=payload.activity_type,
                        activity_id=activity_id,
                        event_count=payload.aggregated_count,
                        occurred_at=occurred_at,
                    ):
                        recorded += 1
    result = dict(state.get("graph_state_result", {}))
    result["activity_exposure"] = {"nodes": len(nodes), "events_recorded": recorded}
    return {"graph_state_result": result}


async def return_no_change(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """exposure 分支固定 no_change（§10.3.1）。"""
    result = dict(state.get("graph_state_result", {}))
    result["status"] = "no_change"
    return {"graph_state_result": result}
