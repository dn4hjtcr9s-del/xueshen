"""MemoryGraphRunner 与 LocalLangGraphRunner（§10.2）。

Runner 只接收 Gateway/Worker 已通过公共 claim_operation 领取的 operation；
Lease、heartbeat、soft/hard timeout 由执行层负责，不由 Graph 节点管理。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Hashable
from typing import Any, Protocol
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from backend.memory.contracts.errors import LeaseFencedError
from backend.memory.contracts.operations import (
    GraphStateChangeView,
    MemoryOperation,
    MemoryOperationResult,
    MutationResult,
)
from backend.memory.graph import (
    activity_exposure,
    batch,
    maintenance,
    manager,
    memory_command,
    projection,
    summary,
)
from backend.memory.graph import graph_state as graph_state_branch
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext

#: Graph 节点签名（State 进、增量更新出）。包装层用 Any 形参：langgraph 的
#: `_NodeWithRuntime[...]` 是结构化兼容的，但 mypy 无法把 Callable 别名"看见"成它，
#: 而这里唯一要保证的是"原样透传 state/runtime"，返回类型仍然是 State 增量。
NodeFn = Callable[..., Awaitable[dict[str, Any]]]


def _guard_member_node(fn: NodeFn) -> NodeFn:
    """成员体节点守卫（评审 I-9）：把单成员异常变成"短路 + 记失败"。

    为什么逐节点包、而不是把成员体合成一个 superstep：§4.1/§4.2 依赖**superstep 级**
    崩溃恢复。`prepare_commit_mutation_plans` 生成的是随机 `mutation_id`，若整块只写一个
    checkpoint，崩溃后只能从成员开头重跑，半提交的成员会重复写入
    （`_member_already_committed` 只跳过"已有 commit 行"的成员，防不住它）。逐节点守卫
    保留每个节点各自是 superstep——崩溃恢复粒度不变。

    只对**批量分支**生效：`batch_active` 为假（单条 operation / 命令 / 维护路径）时原样
    上抛，单条路径行为逐字不变。两类异常不隔离、直接重抛：``LeaseFencedError``（失租信号，
    执行层必须立刻终止旧执行者）与 ``batch.MEMBER_FATAL_EXCEPTIONS``（环境不可用，换一条
    成员重试同样失败，隔离它等于用一次运行把整批判死）。
    """
    node_name = getattr(fn, "__name__", "member_node")

    async def guarded(
        state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
    ) -> dict[str, Any]:
        try:
            result = await fn(state, runtime)
        except Exception as exc:
            if not state.get("batch_active"):
                raise
            if isinstance(exc, LeaseFencedError) or batch.is_member_fatal(exc):
                raise
            return {
                "batch_member_error": {
                    "reason": batch.MEMBER_FAILURE_REASON,
                    "error_type": type(exc).__name__,
                    "message": str(exc)[: batch.MAX_FAILURE_MESSAGE_CHARS],
                    "node": node_name,
                }
            }
        # 本步成功即清掉失败信号：它的唯一用途是触发下面那条短路条件边。
        return {**result, "batch_member_error": {}}

    return guarded


def _route_after_member_node(state: MemoryManagerState) -> str:
    """成员体任一节点之后的统一路由：有失败信号 → 记成员结果；否则走下一个节点。"""
    return "member_failed" if state.get("batch_member_error") else "continue"


def _route_after_route_candidates(state: MemoryManagerState) -> str:
    """`route_candidates` 之后（批量链与单条链共用同一条条件边）。

    失败信号优先；否则按链自身的分支走。单条链的 `route` 语义与改造前逐字一致
    （`summary_finalize` / `summary_process`），批量链的继续分支叫 `continue`。
    """
    if state.get("batch_member_error"):
        return "member_failed"
    # 缺省与改造前逐字一致：`state.get("route", "summary_finalize")`
    if state.get("route", "summary_finalize") == "summary_finalize":
        return "summary_finalize"
    if state.get("batch_active"):
        return "summary_process"
    return "continue"


def _route_after_finalize_summary_result(state: MemoryManagerState) -> str:
    """`finalize_summary_result` 之后（批量链与单条链共用同一条条件边）。

    批量链收尾回成员循环记结果（等价于 `batch.route_after_summary_finalize`），异常短路
    也回 `record_batch_member` 记失败；单条链走 `normalize_result`，逐字不变。
    """
    if not state.get("batch_active"):
        return "continue"
    return "batch_member_done" if not state.get("batch_member_error") else "member_failed"


#: 批量成员体节点名的前缀：与单条路径的同名节点共存，但注册的是守卫版包装。
MEMBER_NODE_PREFIX = "member__"


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

    # 批量总结分支（§4.2 / §5.8）：复用 summary 链逐条处理成员，再进 consolidation 入口
    builder.add_node("load_batch_members", batch.load_batch_members)
    builder.add_node("begin_batch_member", batch.begin_batch_member)
    builder.add_node("record_batch_member", batch.record_batch_member)
    builder.add_node("enter_batch_consolidation", batch.enter_batch_consolidation)
    builder.add_node("finalize_batch_result", batch.finalize_batch_result)

    # 成员体（评审 I-9）：与单条路径**同一批节点函数**，但逐节点套守卫——任一节点抛
    # 非致命异常只写 `batch_member_error`，由条件边短跳到 `record_batch_member`，该成员记
    # batch_failed 后循环继续。保留"每个节点各自一个 superstep"，崩溃恢复粒度不变。
    member_chain = [
        "load_source_refs",
        "sanitize_and_bound_source",
        "extract_candidates",
        "apply_scope_and_value_policy",
        "route_candidates",
        "persist_review_candidates",
        "resolve_existing_memories",
        "resolve_graph_candidates",
        "build_mutation_plan_drafts",
        "prepare_commit_mutation_plans",
        "commit_summary_memories",
        "finalize_summary_result",
    ]
    for member_node_name in member_chain:
        builder.add_node(
            f"{MEMBER_NODE_PREFIX}{member_node_name}",
            _guard_member_node(getattr(summary, member_node_name)),
        )

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
            "batch": "load_batch_members",
            "finalize_replay": "normalize_result",
        },
    )

    # summary 链（单条路径）。`route_candidates` / `finalize_summary_result` 这两个
    # **出口节点**的条件边由批量链统一装配（见下）：同一个节点只能有一条条件边，
    # 批量链的守卫分支必须与链自身的分支写在同一条边里。
    builder.add_edge("load_source_refs", "sanitize_and_bound_source")
    builder.add_edge("sanitize_and_bound_source", "extract_candidates")
    builder.add_edge("extract_candidates", "apply_scope_and_value_policy")
    builder.add_edge("apply_scope_and_value_policy", "route_candidates")
    builder.add_edge("persist_review_candidates", "resolve_existing_memories")
    builder.add_edge("resolve_existing_memories", "resolve_graph_candidates")
    builder.add_edge("resolve_graph_candidates", "build_mutation_plan_drafts")
    builder.add_edge("build_mutation_plan_drafts", "prepare_commit_mutation_plans")
    builder.add_edge("prepare_commit_mutation_plans", "commit_summary_memories")
    builder.add_edge("commit_summary_memories", "finalize_summary_result")

    # 批量链
    builder.add_edge("load_batch_members", "begin_batch_member")
    builder.add_conditional_edges(
        "begin_batch_member",
        batch.route_after_begin_member,
        {
            "member": f"{MEMBER_NODE_PREFIX}load_source_refs",
            "next": "begin_batch_member",
            "consolidate": "enter_batch_consolidation",
        },
    )

    # 成员体链路（守卫版节点）：每个节点后都有"短路到 record_batch_member / 继续"。
    # **出口节点**（route_candidates / finalize_summary_result）的条件边同时承载链自身的
    # 分支：一个节点只能有一条条件边，所以单条路径的边也在这里装配（见上面的 summary 链）。
    def member_node(name: str) -> str:
        return f"{MEMBER_NODE_PREFIX}{name}"

    builder.add_conditional_edges(
        "route_candidates",
        _route_after_route_candidates,
        {
            "summary_finalize": "finalize_summary_result",
            "summary_process": "persist_review_candidates",
            "continue": member_node("persist_review_candidates"),
            "member_failed": "record_batch_member",
        },
    )
    builder.add_conditional_edges(
        member_node("route_candidates"),
        _route_after_route_candidates,
        {
            "summary_finalize": member_node("finalize_summary_result"),
            "summary_process": member_node("persist_review_candidates"),
            "continue": member_node("persist_review_candidates"),
            "member_failed": "record_batch_member",
        },
    )
    for current, following in (
        ("load_source_refs", "sanitize_and_bound_source"),
        ("sanitize_and_bound_source", "extract_candidates"),
        ("extract_candidates", "apply_scope_and_value_policy"),
        ("apply_scope_and_value_policy", "route_candidates"),
        ("persist_review_candidates", "resolve_existing_memories"),
        ("resolve_existing_memories", "resolve_graph_candidates"),
        ("resolve_graph_candidates", "build_mutation_plan_drafts"),
        ("build_mutation_plan_drafts", "prepare_commit_mutation_plans"),
        ("prepare_commit_mutation_plans", "commit_summary_memories"),
        ("commit_summary_memories", "finalize_summary_result"),
    ):
        builder.add_conditional_edges(
            member_node(current),
            _route_after_member_node,
            {"continue": member_node(following), "member_failed": "record_batch_member"},
        )
    # 收尾节点：单条链的 `continue` 与批量链的 `batch_member_done` 共用一个映射表
    # （两者都由 `_route_after_finalize_summary_result` 产出，只在各自分支可达）。
    finish_map: dict[Hashable, str] = {
        "continue": "normalize_result",
        "batch_member_done": "record_batch_member",
        "member_failed": "record_batch_member",
    }
    builder.add_conditional_edges(
        "finalize_summary_result", _route_after_finalize_summary_result, finish_map
    )
    builder.add_conditional_edges(
        member_node("finalize_summary_result"), _route_after_finalize_summary_result, finish_map
    )
    builder.add_conditional_edges(
        "record_batch_member",
        batch.route_after_record_member,
        {"next": "begin_batch_member", "consolidate": "enter_batch_consolidation"},
    )
    builder.add_edge("enter_batch_consolidation", "finalize_batch_result")
    builder.add_edge("finalize_batch_result", "normalize_result")

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
            self._purge_graph if operation.operation_type == "purge_account_memory" else self._graph
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
