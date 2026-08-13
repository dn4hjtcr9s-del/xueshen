"""rewrite_and_plan 节点（方案 §5.2 / §11）。

Structured RewritePlan；服务端规范化 answer_mode × need_retrieval（§11.1）；
非法输出有限重试后降级为"当前问题作为单一检索查询"（§11.2 #9）；
MULTI_QUERY_ENABLED=false 时截断为第 1 条子查询（附录 A.10）。
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from backend.conversation.contracts.graph import RewritePlan
from backend.conversation.graph.state import ConversationRuntimeContext, normalize_plan_mode


async def rewrite_and_plan(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
    context_service: Any,
    vocabulary: Any,
    max_subqueries: int,
) -> dict[str, Any]:
    """执行问题改写与检索规划（§11）。"""
    flags = runtime.flags
    snapshot = state.get("snapshot") or {}
    plan_revision = int(state.get("plan_revision") or 0)
    executed = state.get("executed_query_fingerprints") or []
    missing = (
        (state.get("evidence_assessment") or {}).get("missing_aspects") or []
        if state.get("evidence_assessment")
        else []
    )

    if not flags.get("agentic_rag", True):
        # 附录 A.10：AGENTIC_RAG_ENABLED=false → 当前消息原文单查询
        return _single_query_plan(state, plan_revision)

    snapshot_obj = _snapshot_obj(snapshot)
    view = context_service.build_rewrite_view(
        snapshot=snapshot_obj,
        vocabulary=vocabulary,
        executed_queries=executed,
        missing_aspects=missing,
    )
    raw = await runtime.openai_gateway.rewrite_and_plan(context_view=view, prior_attempts=0)
    plan = RewritePlan.model_validate(raw)
    plan = normalize_plan_mode(plan, max_subqueries=max_subqueries)
    if not flags.get("multi_query", True) and plan.subqueries:
        plan = _keep_first_subquery(plan)
    subquery_ids = _stable_subquery_ids(plan, plan_revision)
    plan.subqueries = [
        subquery.model_copy(update={"subquery_id": subquery_ids[i]})
        for i, subquery in enumerate(plan.subqueries)
    ]
    # §11.1：plan_revision 由服务端分配；Worker Key 与聚合都以此为准
    next_revision = plan_revision + 1
    plan = plan.model_copy(update={"plan_revision": next_revision})
    new_fingerprints = _query_fingerprints(plan)
    return {
        "rewrite_plan": plan.model_dump(mode="json"),
        "plan_revision": next_revision,
        "executed_query_fingerprints": [*executed, *new_fingerprints],
        "degraded_flags": [],
    }


def _snapshot_obj(snapshot: dict[str, Any]) -> Any:
    from backend.conversation.graph.state import snapshot_from_dict

    return snapshot_from_dict(snapshot)


def _single_query_plan(state: dict[str, Any], plan_revision: int) -> dict[str, Any]:
    """降级：standalone=当前消息，强制单子查询进检索（附录 A.10）。"""
    current = str((state.get("snapshot") or {}).get("current_message") or "")
    from backend.conversation.contracts.graph import RetrievalSubquery

    plan = RewritePlan(
        plan_revision=plan_revision,
        standalone_question=current,
        answer_mode="rag",
        need_retrieval=True,
        subqueries=[
            RetrievalSubquery(
                subquery_id="sq-0",
                query_text=current[:500],
                intent="fallback",
                coverage_target="",
                semantic_filters={},
            )
        ],
        reason_codes=["agentic_rag_disabled"],
    )
    next_revision = plan_revision + 1
    plan = plan.model_copy(update={"plan_revision": next_revision})
    return {
        "rewrite_plan": plan.model_dump(mode="json"),
        "plan_revision": next_revision,
        "executed_query_fingerprints": [],
    }


def _keep_first_subquery(plan: RewritePlan) -> RewritePlan:
    return plan.model_copy(update={"subqueries": plan.subqueries[:1]})


def _stable_subquery_ids(plan: RewritePlan, plan_revision: int) -> list[str]:
    """§11.2 #3：subquery_id 由 plan_revision + ordinal + query fingerprint 稳定生成。"""
    ids: list[str] = []
    for ordinal, subquery in enumerate(plan.subqueries):
        digest = sha256(f"{plan_revision}:{ordinal}:{subquery.query_text}".encode()).hexdigest()[
            :12
        ]
        ids.append(f"sq-{plan_revision}-{ordinal}-{digest}")
    return ids


def _query_fingerprints(plan: RewritePlan) -> list[str]:
    """规范化查询指纹（第二轮改写禁止重复旧查询，§11.2 #8）。"""
    return [sha256(s.query_text.strip().lower().encode()).hexdigest() for s in plan.subqueries]
