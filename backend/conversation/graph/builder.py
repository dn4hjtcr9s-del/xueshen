"""ConversationGraph 构建器（方案 §5.2 图 / §4.1 依赖规则）。

节点拓扑（与 §5.2 mermaid 一致）：
START → load_conversation_context → recall_memory → build_turn_snapshot
→ rewrite_and_plan → route(need_retrieval?)
   否 → generate_answer
   是 → embed_subqueries → Send×N retrieve_subquery → aggregate_results
     → deduplicate_and_rerank → evaluate_evidence → route(充分?)
       需补检索且预算内 → rewrite_and_plan（回边，不重建快照）
       否则 → generate_answer
→ validate_answer_and_citations → persist_turn
→（memory_trigger=explicit_remember → explicit_remember_ack）→ END

依赖规则（§4.1）：节点只依赖 Gateway Protocol 与 runtime context；
所有具体客户端在 composition root 装配。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from backend.conversation.contracts.retrieval import ActiveCorpusVocabulary
from backend.conversation.graph.nodes import (
    answer as answer_node,
)
from backend.conversation.graph.nodes import (
    context as context_node,
)
from backend.conversation.graph.nodes import (
    evidence as evidence_node,
)
from backend.conversation.graph.nodes import (
    finalize as finalize_node,
)
from backend.conversation.graph.nodes import (
    memory as memory_node,
)
from backend.conversation.graph.nodes import (
    memory_ack as memory_ack_node,
)
from backend.conversation.graph.nodes import (
    retrieval as retrieval_node,
)
from backend.conversation.graph.nodes import (
    rewrite as rewrite_node,
)
from backend.conversation.graph.nodes import (
    snapshot as snapshot_node,
)
from backend.conversation.graph.progress import emit_progress
from backend.conversation.graph.state import ConversationGraphState, ConversationRuntimeContext


def build_conversation_graph(
    *,
    runtime_context: ConversationRuntimeContext,
    session_factory: Any = None,
    settings: Any = None,
    context_service: Any = None,
    token_counter: Any = None,
    vocabulary: ActiveCorpusVocabulary | None = None,
    logger: logging.Logger | None = None,
) -> StateGraph:
    """编译 ConversationGraph（§5.2）。

    session_factory/settings/context_service/token_counter 可在单测中直注；
    production 由 worker/main 传入完整 runtime。
    """
    logger = logger or runtime_context.logger
    context_service = context_service or runtime_context.context_service
    settings = settings or runtime_context.settings
    token_counter = token_counter or runtime_context.token_counter
    repo = runtime_context.conversation_repository
    if context_service is None or settings is None or token_counter is None:
        raise ValueError(
            "build_conversation_graph 需要 runtime.context_service/settings/token_counter "
            "（composition root 装配）或显式直注"
        )
    max_subqueries = int(settings.conversation_retrieval_max_subqueries)
    vocabulary = vocabulary or ActiveCorpusVocabulary()

    graph: StateGraph = StateGraph(ConversationGraphState)

    # ---------- 节点注册 ----------
    # 注意：节点必须是 async 函数（返回 await 结果），不能返回 coroutine 对象。

    async def _node_load_context(state: ConversationGraphState) -> dict[str, Any]:
        raw_state = dict(state)
        await emit_progress(
            runtime_context,
            raw_state,
            stage="context",
            status="started",
            title="正在整理对话上下文",
            detail="读取本轮问题、最近对话与已有摘要。",
        )
        conversation_context = await context_node.load_conversation_context(
            raw_state,
            session_factory=repo.session_factory,
            max_messages=int(settings.conversation_context_max_messages),
        )
        await emit_progress(
            runtime_context,
            raw_state,
            stage="context",
            status="completed",
            title="已整理对话上下文",
            metadata={
                "history_messages": len(conversation_context.get("recent_messages") or []),
                "has_summary": bool(conversation_context.get("conversation_summary")),
            },
        )
        # 上下文节点返回的是内容字典，必须挂到 Graph State 的 conversation_context 字段，
        # 否则 LangGraph 会丢弃 current_message/recent_messages，回答模型只能看到空问题。
        return {"conversation_context": conversation_context}

    async def _node_recall_memory(state: ConversationGraphState) -> dict[str, Any]:
        raw_state = dict(state)
        await emit_progress(
            runtime_context,
            raw_state,
            stage="memory",
            status="started",
            title="正在读取长期记忆",
            detail="查找与当前问题相关的学习记录和知识状态。",
        )
        result = await memory_node.recall_memory(raw_state, runtime=runtime_context)
        memory_status = str((result.get("memory_context") or {}).get("status") or "unavailable")
        progress_status = "completed"
        title = "已读取相关记忆"
        detail = "相关学习记录已加入本轮上下文。"
        if not runtime_context.flags.get("memory_read", True):
            progress_status = "skipped"
            title = "长期记忆读取未启用"
            detail = "本轮仅使用对话上下文与教材检索结果。"
        elif memory_status == "unavailable":
            progress_status = "degraded"
            title = "长期记忆暂不可用"
            detail = "系统将继续使用对话上下文与教材资料回答。"
        elif memory_status == "degraded":
            progress_status = "degraded"
            title = "已读取部分相关记忆"
            detail = "记忆内容经过预算截断，系统将结合教材资料补充。"
        await emit_progress(
            runtime_context,
            raw_state,
            stage="memory",
            status=progress_status,
            title=title,
            detail=detail,
            metadata={"memory_status": memory_status},
        )
        return result

    async def _node_build_snapshot(state: ConversationGraphState) -> dict[str, Any]:
        return await snapshot_node.build_turn_snapshot(
            dict(state), runtime=runtime_context, context_service=context_service
        )

    async def _node_rewrite(state: ConversationGraphState) -> dict[str, Any]:
        raw_state = dict(state)
        await emit_progress(
            runtime_context,
            raw_state,
            stage="rewrite",
            status="started",
            title="正在理解并改写问题",
            detail="把当前问题整理成适合检索和回答的独立问题。",
        )
        result = await rewrite_node.rewrite_and_plan(
            raw_state,
            runtime=runtime_context,
            context_service=context_service,
            vocabulary=vocabulary,
            max_subqueries=max_subqueries,
        )
        plan = result.get("rewrite_plan") or {}
        standalone = str(plan.get("standalone_question") or "").strip()
        subqueries = plan.get("subqueries") or []
        await emit_progress(
            runtime_context,
            raw_state,
            stage="rewrite",
            status="completed",
            title="已完成问题改写",
            detail=standalone or "已生成本轮回答计划。",
            metadata={
                "need_retrieval": bool(plan.get("need_retrieval")),
                "subquery_count": len(subqueries),
                "plan_revision": int(plan.get("plan_revision") or 0),
            },
        )
        # C6（第三轮必改 3）：retrieval_iteration 统计**已执行的补检索轮数**——
        # 仅在非首轮（state 已有上一轮 evidence_assessment）时 +1；
        # 首轮改写不计数。路由判定 iteration >= max_iterations：默认
        # max_iterations=1 表示允许 1 次补检索（§14.3 表），与需求一致。
        iteration = int(state.get("retrieval_iteration") or 0)
        if state.get("evidence_assessment") is not None:
            iteration += 1
        result["retrieval_iteration"] = iteration
        result["_max_retrieval_iterations"] = int(settings.conversation_retrieval_max_iterations)
        return result

    async def _node_embed(state: ConversationGraphState) -> dict[str, Any]:
        raw_state = dict(state)
        subqueries = (raw_state.get("rewrite_plan") or {}).get("subqueries") or []
        await emit_progress(
            runtime_context,
            raw_state,
            stage="retrieval",
            status="started",
            title="正在准备教材检索",
            detail=f"为 {len(subqueries)} 个检索问题生成向量表示。",
            metadata={"subquery_count": len(subqueries)},
        )
        try:
            result = await retrieval_node.embed_subqueries(raw_state, runtime=runtime_context)
        except Exception:
            await emit_progress(
                runtime_context,
                raw_state,
                stage="retrieval",
                status="degraded",
                title="检索准备暂时失败",
                detail="未能生成检索向量，本轮回答已停止。",
                metadata={"subquery_count": len(subqueries)},
            )
            raise
        await emit_progress(
            runtime_context,
            raw_state,
            stage="retrieval",
            status="completed",
            title="教材检索准备完成",
            metadata={"subquery_count": len(subqueries)},
        )
        return result

    async def _node_retrieve(worker_input: dict[str, Any]) -> dict[str, Any]:
        query_text = str(worker_input.get("query_text") or "")
        await emit_progress(
            runtime_context,
            worker_input,
            stage="retrieval",
            status="started",
            title="正在检索相关教材",
            detail=query_text,
            metadata={"subquery_id": str(worker_input.get("subquery_id") or "")},
        )
        try:
            result = await retrieval_node.retrieve_subquery(worker_input, runtime=runtime_context)
        except Exception:
            await emit_progress(
                runtime_context,
                worker_input,
                stage="retrieval",
                status="degraded",
                title="一组教材检索暂时失败",
                detail=query_text,
            )
            raise
        worker_results = result.get("worker_results") or {}
        first: dict[str, Any] = next(iter(worker_results.values()), {})
        hits = first.get("hits") or []
        retrieval_status = str(first.get("status") or "unknown")
        await emit_progress(
            runtime_context,
            worker_input,
            stage="retrieval",
            status="completed" if retrieval_status == "succeeded" else "degraded",
            title="已完成一组教材检索",
            detail=query_text,
            metadata={
                "subquery_id": str(worker_input.get("subquery_id") or ""),
                "hit_count": len(hits),
                "retrieval_status": retrieval_status,
            },
        )
        return result

    async def _node_aggregate(state: ConversationGraphState) -> dict[str, Any]:
        return await evidence_node.aggregate_results(dict(state), runtime=runtime_context)

    async def _node_rerank(state: ConversationGraphState) -> dict[str, Any]:
        raw_state = dict(state)
        await emit_progress(
            runtime_context,
            raw_state,
            stage="rerank",
            status="started",
            title="正在去重并重排证据",
            detail="合并重复片段，并优先保留覆盖问题更完整的资料。",
            metadata={"raw_hit_count": len(raw_state.get("evidence_hits") or [])},
        )
        result = await evidence_node.deduplicate_and_rerank(
            raw_state,
            runtime=runtime_context,
            settings=settings,
            token_counter=token_counter,
        )
        evidence_set = result.get("evidence_set") or {}
        await emit_progress(
            runtime_context,
            raw_state,
            stage="rerank",
            status="completed",
            title="已完成证据重排",
            metadata={
                "evidence_count": len(evidence_set.get("items") or []),
                "evidence_tokens": int(evidence_set.get("total_tokens") or 0),
            },
        )
        return result

    async def _node_evaluate(state: ConversationGraphState) -> dict[str, Any]:
        raw_state = dict(state)
        await emit_progress(
            runtime_context,
            raw_state,
            stage="evidence",
            status="started",
            title="正在检查证据是否充分",
            detail="判断现有资料能否完整、可靠地回答问题。",
        )
        result = await evidence_node.evaluate_evidence(raw_state, runtime=runtime_context)
        assessment = result.get("evidence_assessment") or {}
        assessment_status = str(assessment.get("status") or "unknown")
        titles = {
            "sufficient": "证据充分，准备回答",
            "needs_more": "证据仍有缺口，准备补充检索",
            "insufficient": "证据有限，将明确说明边界",
        }
        await emit_progress(
            runtime_context,
            raw_state,
            stage="evidence",
            status="completed" if assessment_status == "sufficient" else "degraded",
            title=titles.get(assessment_status, "已完成证据检查"),
            metadata={
                "assessment": assessment_status,
                "missing_aspects": len(assessment.get("missing_aspects") or []),
            },
        )
        return result

    async def _node_answer(state: ConversationGraphState) -> dict[str, Any]:
        raw_state = dict(state)
        evidence_count = len((raw_state.get("evidence_set") or {}).get("items") or [])
        await emit_progress(
            runtime_context,
            raw_state,
            stage="answer",
            status="started",
            title="正在组织回答",
            detail="结合对话上下文、可用记忆与检索证据生成讲解。",
            metadata={"evidence_count": evidence_count},
        )
        result = await answer_node.generate_answer(
            raw_state, runtime=runtime_context, context_service=context_service
        )
        await emit_progress(
            runtime_context,
            raw_state,
            stage="answer",
            status="completed",
            title="回答内容已生成",
            metadata={
                "answer_chars": len(str((result.get("answer_payload") or {}).get("answer") or "")),
            },
        )
        return result

    async def _node_validate(state: ConversationGraphState) -> dict[str, Any]:
        return await answer_node.validate_answer_and_citations(dict(state), runtime=runtime_context)

    async def _node_finalize(state: ConversationGraphState) -> dict[str, Any]:
        return await finalize_node.persist_turn(dict(state), runtime=runtime_context)

    async def _node_memoryack(state: ConversationGraphState) -> dict[str, Any]:
        return await memory_ack_node.explicit_remember_ack(dict(state), runtime=runtime_context)

    graph.add_node("load_conversation_context", _node_load_context)
    graph.add_node("recall_memory", _node_recall_memory)
    graph.add_node("build_turn_snapshot", _node_build_snapshot)
    graph.add_node("rewrite_and_plan", _node_rewrite)
    graph.add_node("embed_subqueries", _node_embed)
    graph.add_node("retrieve_subquery", _node_retrieve)
    graph.add_node("aggregate_results", _node_aggregate)
    graph.add_node("deduplicate_and_rerank", _node_rerank)
    graph.add_node("evaluate_evidence", _node_evaluate)
    graph.add_node("generate_answer", _node_answer)
    graph.add_node("validate_answer_and_citations", _node_validate)
    graph.add_node("persist_turn", _node_finalize)
    graph.add_node("explicit_remember_ack", _node_memoryack)

    # ---------- 边 ----------
    graph.add_edge(START, "load_conversation_context")
    graph.add_edge("load_conversation_context", "recall_memory")
    graph.add_edge("recall_memory", "build_turn_snapshot")
    graph.add_edge("build_turn_snapshot", "rewrite_and_plan")
    graph.add_conditional_edges(
        "rewrite_and_plan",
        _route_need_retrieval,
        {"retrieve": "embed_subqueries", "answer": "generate_answer"},
    )
    graph.add_edge("embed_subqueries", "dispatch_retrieval_workers")
    # §5.2：Send × N —— 每个子问题一个独立 Worker（§10.3 Map Reducer 合并）

    async def _dispatch_retrieval_workers(state: dict[str, Any]) -> dict[str, Any]:
        return state

    def _fanout_workers(state: dict[str, Any]) -> list[Send]:
        plan = state.get("rewrite_plan") or {}
        plan_revision = int(plan.get("plan_revision") or 0)
        embedded = state.get("embedded_queries") or {}
        subqueries = plan.get("subqueries") or []
        sends: list[Send] = []
        for subquery in subqueries:
            sends.append(
                Send(
                    "retrieve_subquery",
                    {
                        "turn_id": state.get("turn_id"),
                        "request_id": state.get("request_id"),
                        "run_id": state.get("run_id"),
                        "plan_revision": plan_revision,
                        "subquery_id": str(subquery["subquery_id"]),
                        "query_text": str(subquery["query_text"]),
                        "query_vector": embedded.get(str(subquery["subquery_id"])),
                        "validated_filters": subquery.get("semantic_filters") or {},
                        "limit": int(settings.conversation_retrieval_result_limit),
                    },
                )
            )
        return sends

    graph.add_node("dispatch_retrieval_workers", _dispatch_retrieval_workers)
    graph.add_conditional_edges(
        "dispatch_retrieval_workers",
        _fanout_workers,
        ["retrieve_subquery"],
    )
    graph.add_edge("retrieve_subquery", "aggregate_results")
    graph.add_edge("aggregate_results", "deduplicate_and_rerank")
    graph.add_edge("deduplicate_and_rerank", "evaluate_evidence")
    graph.add_conditional_edges(
        "evaluate_evidence",
        _route_evidence_sufficiency,
        {
            "answer": "generate_answer",
            "replan": "rewrite_and_plan",
            "insufficient": "generate_answer",
        },
    )
    graph.add_edge("generate_answer", "validate_answer_and_citations")
    graph.add_edge("validate_answer_and_citations", "persist_turn")
    graph.add_conditional_edges(
        "persist_turn",
        _route_after_finalize,
        {"end": END, "memoryack": "explicit_remember_ack"},
    )
    graph.add_edge("explicit_remember_ack", END)

    return graph


def _route_need_retrieval(state: dict[str, Any]) -> str:
    """§5.2 ROUTE：need_retrieval? → retrieve / answer。"""
    plan = state.get("rewrite_plan") or {}
    if plan.get("need_retrieval"):
        return "retrieve"
    return "answer"


def _route_evidence_sufficiency(state: dict[str, Any]) -> str:
    """§5.2 ENOUGH：充分/预算耗尽 → answer；需要补检索 → replan（回边）。

    修复（评审 C6）：retrieval_iteration ≥ max_iterations（§14.3 预算）时
    强制进入回答，模型持续输出 needs_more 也不会无限循环。
    """
    assessment = state.get("evidence_assessment") or {}
    status = assessment.get("status")
    if status == "needs_more":
        # 第三轮必改 3：iteration 统计的是**已执行的补检索回边次数**。
        # 判定 iteration > max_iterations：默认 max_iterations=1 允许 1 次补检索。
        iteration = int(state.get("retrieval_iteration") or 0)
        max_iterations = int(state.get("_max_retrieval_iterations") or 1)
        if iteration >= max_iterations:
            return "answer"
        return "replan"
    if status == "insufficient":
        return "insufficient"
    return "answer"


def _route_after_finalize(state: dict[str, Any]) -> str:
    """§5.2 FINALIZE 分支：explicit_remember → MEMORYACK，否则 END。"""
    plan = state.get("rewrite_plan") or {}
    if plan.get("memory_trigger") == "explicit_remember":
        return "memoryack"
    return "end"
