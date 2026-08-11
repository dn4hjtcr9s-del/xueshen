"""KnowledgeGraphStateGraph 分支（§10.5）：用户图谱命令，不调用 OpenAI。"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from backend.memory.contracts.commands import GraphStateCommand
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext


async def run_graph_state(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """validate_node → load_overlay → resolve_user_transition → commit_overlay
    → emit_graph_state_changed（转换与审计在 KnowledgeGraphStateService 内完成）。"""
    ctx = runtime.context
    operation = MemoryOperation.model_validate(state["operation"])
    payload = operation.payload
    assert isinstance(payload, GraphStateCommand)
    # validate_node：节点必须存在于固定注册表
    async with ctx.session_factory() as session:
        registry = ctx.graph_registry_factory(session)
        if not await registry.node_exists(payload.node_id):
            from backend.memory.contracts.errors import GraphNodeNotFoundError

            raise GraphNodeNotFoundError(payload.node_id)
    change = await ctx.graph_state_service.apply_user_command(
        operation_id=operation.operation_id,
        user_id=operation.user_id,
        actor_type=operation.actor_type,
        node_id=payload.node_id,
        action=payload.action,
        expected_version=payload.expected_version,
    )
    return {"graph_state_result": {"changes": [change.model_dump(mode="json")]}}
