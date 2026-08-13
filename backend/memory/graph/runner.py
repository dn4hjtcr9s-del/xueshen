"""MemoryGraphRunner 与 LocalLangGraphRunner（§10.2）。

Runner 只接收 Gateway/Worker 已通过公共 claim_operation 领取的 operation；
Lease、heartbeat、soft/hard timeout 由执行层负责，不由 Graph 节点管理。
"""

from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.memory.contracts.operations import (
    GraphStateChangeView,
    MemoryOperation,
    MemoryOperationResult,
    MutationResult,
)
from backend.memory.graph import (
    activity_exposure,
    maintenance,
    manager,
    memory_command,
    projection,
    summary,
)
from backend.memory.graph import graph_state as graph_state_branch
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext


class MemoryGraphRunner(Protocol):
    async def run(
        self, operation: MemoryOperation, *, fencing: dict[str, Any] | None = None
    ) -> MemoryOperationResult:
        """执行已经成功领取的记忆操作并返回结构化结果。

        fencing（评审二轮 #3）：{"worker_id", "generation"}，经 Graph state
        传到 commit 入口做 CAS；直调路径（测试/维护）可为 None。
        """
        ...


def build_memory_manager_graph(
    checkpointer: BaseCheckpointSaver[Any] | None = None,
) -> CompiledStateGraph[Any, Any, Any, Any]:
    """组装父图 + 分支（§10.3–§10.7）。"""
    builder = StateGraph(MemoryManagerState, context_schema=MemoryRuntimeContext)

    # 父图前置节点
    builder.add_node("normalize_input", manager.normalize_input)
    builder.add_node("authorize_actor", manager.authorize_actor)
    builder.add_node("idempotency_guard", manager.idempotency_guard)
    builder.add_node("validate_invariants", manager.validate_invariants)
    builder.add_node("route_operation", manager.route_operation)
    builder.add_node("normalize_result", manager.normalize_result)

    # summary 分支（§10.4）
    builder.add_node("load_source_refs", summary.load_source_refs)
    builder.add_node("sanitize_and_bound_source", summary.sanitize_and_bound_source)
    builder.add_node("extract_candidates", summary.extract_candidates)
    builder.add_node("apply_scope_and_value_policy", summary.apply_scope_and_value_policy)
    builder.add_node("route_candidates", summary.route_candidates)
    builder.add_node("persist_review_candidates", summary.persist_review_candidates)
    builder.add_node("resolve_existing_memories", summary.resolve_existing_memories)
    builder.add_node("resolve_graph_candidates", summary.resolve_graph_candidates)
    builder.add_node("build_mutation_plan_drafts", summary.build_mutation_plan_drafts)
    builder.add_node("prepare_commit_mutation_plans", summary.prepare_commit_mutation_plans)
    builder.add_node("commit_summary_memories", summary.commit_summary_memories)
    builder.add_node("finalize_summary_result", summary.finalize_summary_result)

    # activity exposure 分支（§10.3.1）
    builder.add_node("validate_activity_hints", activity_exposure.validate_activity_hints)
    builder.add_node("upsert_graph_node_activity", activity_exposure.upsert_graph_node_activity)
    builder.add_node("return_no_change", activity_exposure.return_no_change)

    # 其余分支
    builder.add_node("run_memory_command", memory_command.run_memory_command)
    builder.add_node("run_graph_state", graph_state_branch.run_graph_state)
    builder.add_node("run_projection", projection.run_projection)
    builder.add_node("run_maintenance", maintenance.run_maintenance)

    # 前置链
    builder.add_edge(START, "normalize_input")
    builder.add_edge("normalize_input", "authorize_actor")
    builder.add_edge("authorize_actor", "idempotency_guard")
    builder.add_edge("idempotency_guard", "validate_invariants")
    builder.add_edge("validate_invariants", "route_operation")

    # 路由（§10.3.1 分流在 route_operation 内完成）
    builder.add_conditional_edges(
        "route_operation",
        lambda state: state.get("route", "summary"),
        {
            "summary": "load_source_refs",
            "activity_exposure": "validate_activity_hints",
            "memory_command": "run_memory_command",
            "graph_state": "run_graph_state",
            "projection": "run_projection",
            "maintenance": "run_maintenance",
            "finalize_replay": "normalize_result",
        },
    )

    # summary 链
    builder.add_edge("load_source_refs", "sanitize_and_bound_source")
    builder.add_edge("sanitize_and_bound_source", "extract_candidates")
    builder.add_edge("extract_candidates", "apply_scope_and_value_policy")
    builder.add_edge("apply_scope_and_value_policy", "route_candidates")
    builder.add_conditional_edges(
        "route_candidates",
        lambda state: state.get("route", "summary_finalize"),
        {
            "summary_finalize": "finalize_summary_result",
            "summary_process": "persist_review_candidates",
        },
    )
    builder.add_edge("persist_review_candidates", "resolve_existing_memories")
    builder.add_edge("resolve_existing_memories", "resolve_graph_candidates")
    builder.add_edge("resolve_graph_candidates", "build_mutation_plan_drafts")
    builder.add_edge("build_mutation_plan_drafts", "prepare_commit_mutation_plans")
    builder.add_edge("prepare_commit_mutation_plans", "commit_summary_memories")
    builder.add_edge("commit_summary_memories", "finalize_summary_result")
    builder.add_edge("finalize_summary_result", "normalize_result")

    # activity exposure 链
    builder.add_edge("validate_activity_hints", "upsert_graph_node_activity")
    builder.add_edge("upsert_graph_node_activity", "return_no_change")
    builder.add_edge("return_no_change", "normalize_result")

    # 单节点分支
    builder.add_edge("run_memory_command", "normalize_result")
    builder.add_edge("run_graph_state", "normalize_result")
    builder.add_edge("run_projection", "normalize_result")
    builder.add_edge("run_maintenance", "normalize_result")

    builder.add_edge("normalize_result", END)
    return builder.compile(checkpointer=checkpointer)


class LocalLangGraphRunner:
    """第一版唯一执行器实现（§10.2）。"""

    def __init__(
        self,
        *,
        context: MemoryRuntimeContext,
        checkpointer: BaseCheckpointSaver[Any] | None = None,
    ) -> None:
        self._context = context
        self._graph = build_memory_manager_graph(checkpointer)
        # purge 节点会删除目标用户全部 checkpoint（含当前 purge thread）。若当前
        # Graph 自身仍挂 checkpointer，LangGraph 在后续 normalize_result/END 阶段
        # 会把该 thread 再次写回。专用无 checkpointer 图保证最终物理状态不反弹。
        self._purge_graph = (
            build_memory_manager_graph(None) if checkpointer is not None else self._graph
        )

    async def run(
        self, operation: MemoryOperation, *, fencing: dict[str, Any] | None = None
    ) -> MemoryOperationResult:
        from langchain_core.runnables import RunnableConfig

        from backend.memory.worker.checkpoint import thread_id_for_operation

        # Graph thread 固定为 memory-op:{operation_id}（§11.4）
        config = RunnableConfig(
            configurable={"thread_id": thread_id_for_operation(operation.operation_id)}
        )
        graph = (
            self._purge_graph
            if operation.operation_type == "purge_account_memory"
            else self._graph
        )
        final_state = await graph.ainvoke(
            {"operation": operation.model_dump(mode="json"), "fencing": fencing},
            config=config,
            context=self._context,
        )
        return _to_result(operation, final_state)


def _to_result(operation: MemoryOperation, state: dict[str, Any]) -> MemoryOperationResult:
    raw = state.get("commit_result", {})
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return MemoryOperationResult(
        operation_id=operation.operation_id,
        status=raw.get("status", "succeeded"),
        operation_type=operation.operation_type,
        created_at=operation.occurred_at,
        updated_at=now,
        completed_at=now,
        mutations=[MutationResult.model_validate(m) for m in raw.get("mutations", [])],
        review_candidate_ids=[UUID(str(c)) for c in raw.get("review_candidate_ids", [])],
        graph_state_changes=[
            GraphStateChangeView.model_validate(c) for c in raw.get("graph_state_changes", [])
        ],
        warnings=list(raw.get("warnings", [])),
    )
