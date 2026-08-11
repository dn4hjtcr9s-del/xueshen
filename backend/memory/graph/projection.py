"""Summary Projection 分支（§10.6）。

约束（在 KnowledgeGraphStateService 内强制执行）：
- apply_active_version 确认 source_version 仍为活动版本；过期投递幂等成功。
- recompute_without_deleted_version 排除删除版本，从剩余活动证据重算。
- 无候选节点/无可靠映射/证据不足时 no_change 成功结束。
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime

from backend.memory.contracts.commands import ProjectSummaryToGraphCommand
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext


async def run_projection(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    ctx = runtime.context
    operation = MemoryOperation.model_validate(state["operation"])
    payload = operation.payload
    assert isinstance(payload, ProjectSummaryToGraphCommand)
    outcome = await ctx.graph_state_service.apply_projection(
        operation_id=operation.operation_id,
        user_id=operation.user_id,
        command=payload,
    )
    result: dict[str, Any] = {"changed": outcome.changed}
    if outcome.change is not None:
        result["changes"] = [outcome.change.model_dump(mode="json")]
    warnings = list(state.get("warnings", []))
    if outcome.warning:
        warnings.append(outcome.warning)
    return {"graph_state_result": result, "warnings": warnings}
