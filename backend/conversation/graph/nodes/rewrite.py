"""rewrite_and_plan 节点（方案 §5.2 / §11）。

改写 Agent 同时负责问题改写、教材检索裁决和子问题生成；服务端只校验裁决一致性，
不把模型未决定的请求静默改造成 RAG。唯一例外是补检索轮：上一轮已裁决 retrieve
（evidence_assessment 存在）时，模型改判 skip/clarify 视为与证据缺口矛盾，
按缺失方面强制继续检索。未来 Memory Tool 仍由同一个 Agent 在裁决前主动调用，
当前阶段直接使用快照中已经读取的全部记忆。
"""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from pydantic import ValidationError

from backend.conversation.contracts.errors import ModelUnavailableError
from backend.conversation.contracts.graph import (
    RetrievalBasisCode,
    RetrievalDecision,
    RewritePlan,
)
from backend.conversation.graph.state import ConversationRuntimeContext, normalize_plan_mode
from backend.conversation.rollout.recorder import record_rollout


async def rewrite_and_plan(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
    context_service: Any,
    vocabulary: Any,
    max_subqueries: int,
) -> dict[str, Any]:
    """执行问题改写、检索裁决和子问题规划（§11）。"""
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
        # 规划功能关闭时保守跳过教材检索：无法可靠判断教材必要性时，不默认启动检索。
        return _conservative_plan(
            state,
            plan_revision,
            reason_code="agentic_rag_disabled",
            basis_code="PLANNER_DISABLED",
            rationale="当前检索规划功能未启用，无法可靠判断教材是否必要，因此本轮不启动教材检索。",
        )

    snapshot_obj = _snapshot_obj(snapshot)
    view = context_service.build_rewrite_view(
        snapshot=snapshot_obj,
        vocabulary=vocabulary,
        executed_queries=executed,
        missing_aspects=missing,
    )
    try:
        raw = await runtime.openai_gateway.rewrite_and_plan(
            context_view=view,
            prior_attempts=0,
        )
    except ModelUnavailableError as exc:
        runtime.logger.warning(
            "Rewrite Structured Output 保守降级为不检索 reason=%s attempts=%s",
            getattr(exc, "reason", "model_unavailable"),
            getattr(exc, "attempts", None),
        )
        return _conservative_plan(
            state,
            plan_revision,
            reason_code="rewrite_structured_fallback",
            basis_code="PLANNER_UNAVAILABLE",
            rationale="改写与检索规划暂时不可用，为避免在无法判断需求时默认检索，本轮先直接回答。",
            degraded_flags=["rewrite_structured_fallback"],
        )
    # 网关调用的异常只处理模型不可用；网关自身实现异常不得被误标为"契约不一致"。
    try:
        plan = RewritePlan.model_validate(raw)
        plan = normalize_plan_mode(plan, max_subqueries=max(1, max_subqueries))
    except (ValidationError, ValueError) as exc:
        # 真实 Gateway 会在结构化输出层重试；兼容 Gateway/Fake 直接返回矛盾结果时，
        # 这里仍采用同一保守策略，绝不把不一致输出静默改成检索。
        runtime.logger.warning("RewritePlan 契约不一致，保守降级为不检索: %s", str(exc)[:200])
        return _conservative_plan(
            state,
            plan_revision,
            reason_code="rewrite_plan_contract_invalid",
            basis_code="PLANNER_UNAVAILABLE",
            rationale="改写结果未通过检索裁决一致性校验，为避免误检索，本轮先直接回答。",
            degraded_flags=["rewrite_plan_contract_invalid"],
        )

    # 补检索轮硬约束：上一轮已确认需要教材检索（evidence_assessment 存在），
    # 本轮模型若改判 skip/clarify，与"上一轮证据不足"矛盾，按缺失方面强制继续检索。
    if (
        state.get("evidence_assessment") is not None
        and plan.retrieval_decision.decision != "retrieve"
    ):
        runtime.logger.warning(
            "补检索轮改判 decision=%s，按硬约束强制 retrieve（missing_aspects=%s）",
            plan.retrieval_decision.decision,
            len(missing),
        )
        plan = _force_replan_retrieve(plan, missing_aspects=missing, max_subqueries=max_subqueries)

    if not flags.get("multi_query", True) and plan.subqueries:
        plan = _keep_first_subquery(plan)
    subquery_ids = _stable_subquery_ids(plan, plan_revision)
    plan.subqueries = [
        subquery.model_copy(update={"subquery_id": subquery_ids[i]})
        for i, subquery in enumerate(plan.subqueries)
    ]
    # §11.1：plan_revision 由服务端分配；Worker Key 与聚合都以此为准。
    next_revision = plan_revision + 1
    plan = plan.model_copy(update={"plan_revision": next_revision})
    new_fingerprints = _query_fingerprints(plan)
    # memory-rebuild §5.3 写入顺序第 4 条：rewrite_plan 每个 revision 落一条
    # （每 revision，而不是每轮一条——补检索会再改写一次）。
    await record_rollout(
        runtime,
        "rewrite_plan",
        {"revision": next_revision, "plan": plan.model_dump(mode="json")},
    )
    return {
        "rewrite_plan": plan.model_dump(mode="json"),
        "plan_revision": next_revision,
        "executed_query_fingerprints": [*executed, *new_fingerprints],
        "degraded_flags": [],
    }


def _snapshot_obj(snapshot: dict[str, Any]) -> Any:
    from backend.conversation.graph.state import snapshot_from_dict

    return snapshot_from_dict(snapshot)


def _force_replan_retrieve(
    plan: RewritePlan,
    *,
    missing_aspects: list[str],
    max_subqueries: int,
) -> RewritePlan:
    """补检索轮强制检索：按缺失方面生成子查询，正面响应上一轮的证据缺口。

    仅当上一轮已裁决 retrieve（evidence_assessment 存在）时触发；服务端不
    静默修改检索语义，而是在 rationale 中向用户说明本次强制继续检索的原因。
    """
    from backend.conversation.contracts.graph import RetrievalSubquery

    aspects = [str(a).strip() for a in missing_aspects if str(a).strip()]
    if not aspects:
        aspects = [plan.standalone_question]
    subqueries = [
        RetrievalSubquery(
            subquery_id=f"sq-replan-{i}",
            query_text=aspect[:500],
            intent="supplement",
            coverage_target="",
            semantic_filters={},
        )
        for i, aspect in enumerate(aspects[: max(max_subqueries, 1)])
    ]
    return plan.model_copy(
        update={
            "answer_mode": "rag",
            "need_retrieval": True,
            "retrieval_decision": RetrievalDecision(
                decision="retrieve",
                basis_codes=["TEXTBOOK_FACT_REQUIRED", "CURRENT_CONTEXT_INSUFFICIENT"],
                rationale=(
                    "上一轮证据评估显示教材证据不足；本轮若不再检索会与证据缺口矛盾，"
                    "因此按补检索轮约束，用缺失方面继续检索教材。"
                ),
            ),
            "subqueries": subqueries,
            "reason_codes": [*plan.reason_codes, "replan_forced_retrieve"],
        }
    )


def _conservative_plan(
    state: dict[str, Any],
    plan_revision: int,
    *,
    reason_code: str,
    basis_code: RetrievalBasisCode,
    rationale: str,
    degraded_flags: list[str] | None = None,
) -> dict[str, Any]:
    """规划失败时生成不检索计划，不用当前问题强行充当教材查询。"""
    current = str((state.get("snapshot") or {}).get("current_message") or "").strip()
    standalone_question = (current or "请回答当前用户问题")[:1000]
    plan = RewritePlan(
        plan_revision=plan_revision,
        standalone_question=standalone_question,
        answer_mode="direct",
        need_retrieval=False,
        retrieval_decision=RetrievalDecision(
            decision="skip",
            basis_codes=[basis_code],
            rationale=rationale,
        ),
        subqueries=[],
        reason_codes=[reason_code],
    )
    next_revision = plan_revision + 1
    plan = plan.model_copy(update={"plan_revision": next_revision})
    return {
        "rewrite_plan": plan.model_dump(mode="json"),
        "plan_revision": next_revision,
        "executed_query_fingerprints": list(state.get("executed_query_fingerprints") or []),
        "degraded_flags": degraded_flags or [],
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
