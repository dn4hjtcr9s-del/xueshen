"""检索裁决契约与保守降级的单元测试（改写策略改版后新增）。

覆盖：
1. RewritePlan 校验器对裁决一致性的硬约束；
2. normalize_plan_mode 的边界裁剪；
3. rewrite 节点收到矛盾计划时的保守降级（不检索）；
4. 补检索轮改判 skip/clarify 时服务端强制 retrieve。
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from backend.conversation.contracts.graph import RewritePlan
from backend.conversation.contracts.retrieval import ActiveCorpusVocabulary
from backend.conversation.graph.nodes.rewrite import rewrite_and_plan
from backend.conversation.graph.state import normalize_plan_mode
from tests.conversation.graph_fixtures import build_runtime


def _subquery(text: str = "根值判别法", subquery_id: str = "sq-0") -> dict[str, Any]:
    return {
        "subquery_id": subquery_id,
        "query_text": text,
        "intent": "definition",
        "coverage_target": "",
        "semantic_filters": {},
    }


def _plan(**overrides: Any) -> RewritePlan:
    base: dict[str, Any] = {
        "plan_revision": 0,
        "standalone_question": "根值判别法的适用条件是什么？",
        "answer_mode": "rag",
        "need_retrieval": True,
        "retrieval_decision": {
            "decision": "retrieve",
            "basis_codes": ["TEXTBOOK_FACT_REQUIRED"],
            "rationale": "用户询问教材事实，需要检索后回答。",
        },
        "subqueries": [_subquery()],
    }
    base.update(overrides)
    return RewritePlan.model_validate(base)


def test_consistent_retrieve_plan_accepts() -> None:
    plan = _plan()
    assert plan.retrieval_decision.decision == "retrieve"
    assert len(plan.subqueries) == 1


def test_consistent_skip_plan_accepts() -> None:
    plan = _plan(
        answer_mode="direct",
        need_retrieval=False,
        retrieval_decision={
            "decision": "skip",
            "basis_codes": ["USER_STATE_TARGET", "TEXTBOOK_NOT_EVIDENCE_FOR_USER_STATE"],
            "rationale": "用户询问个人掌握情况，教材不能证明用户掌握了什么。",
        },
        subqueries=[],
    )
    assert plan.need_retrieval is False


def test_retrieve_without_subqueries_rejected() -> None:
    with pytest.raises(ValidationError):
        _plan(subqueries=[])


def test_skip_with_subqueries_rejected() -> None:
    with pytest.raises(ValidationError):
        _plan(
            answer_mode="direct",
            need_retrieval=False,
            retrieval_decision={
                "decision": "skip",
                "basis_codes": ["CONVERSATION_CONTEXT_SUFFICIENT"],
                "rationale": "当前上下文足以直接回答。",
            },
        )


def test_retrieve_with_rag_mode_but_no_need_flag_rejected() -> None:
    with pytest.raises(ValidationError):
        _plan(need_retrieval=False)


def test_clarify_requires_no_subqueries() -> None:
    plan = _plan(
        answer_mode="direct",
        need_retrieval=False,
        retrieval_decision={
            "decision": "clarify",
            "basis_codes": ["AMBIGUOUS_REQUEST"],
            "rationale": "存在多个候选指代对象，需要先澄清。",
        },
        subqueries=[],
    )
    assert plan.retrieval_decision.decision == "clarify"


def test_normalize_clips_subqueries_but_keeps_decision() -> None:
    plan = _plan(subqueries=[_subquery(text=f"查询{i}", subquery_id=f"sq-{i}") for i in range(5)])
    clipped = normalize_plan_mode(plan, max_subqueries=2)
    assert [sq.query_text for sq in clipped.subqueries] == ["查询0", "查询1"]
    assert clipped.retrieval_decision.decision == "retrieve"


def test_normalize_rejects_retrieve_truncated_to_empty() -> None:
    plan = _plan()
    with pytest.raises(ValueError):
        normalize_plan_mode(plan, max_subqueries=0)


class InconsistentPlanGateway:
    """返回与裁决矛盾的裸 dict（模拟 Gateway 绕过 Schema 校验，走 model_validate 拒绝）。"""

    async def rewrite_and_plan(
        self, *, context_view: dict[str, Any], prior_attempts: int
    ) -> dict[str, Any]:
        return {
            "plan_revision": 0,
            "standalone_question": "帮我看看这张图",
            "answer_mode": "direct",
            "need_retrieval": False,
            "retrieval_decision": {
                "decision": "skip",
                "basis_codes": ["CONVERSATION_CONTEXT_SUFFICIENT"],
                "rationale": "当前上下文足以直接回答。",
            },
            "subqueries": [
                {
                    "subquery_id": "sq-0",
                    "query_text": "不该残留的子查询",
                    "intent": "definition",
                    "coverage_target": "",
                    "semantic_filters": {},
                }
            ],
        }


class BadGatewayImpl:
    """网关自身实现缺陷异常（与契约校验无关），应向上传播而不被误吞。"""

    async def rewrite_and_plan(
        self, *, context_view: dict[str, Any], prior_attempts: int
    ) -> dict[str, Any]:
        raise RuntimeError("gateway 内部实现错误")


async def test_rewrite_node_conservative_fallback_on_inconsistent_plan() -> None:
    runtime = build_runtime(openai_gateway=InconsistentPlanGateway())
    state = {
        "snapshot": {
            "snapshot_id": "test-snapshot",
            "current_message": "帮我看看这张图",
            "recent_messages": [],
            "conversation_summary": None,
        },
        "plan_revision": 0,
        "executed_query_fingerprints": [],
    }

    result = await rewrite_and_plan(
        state,
        runtime=runtime,
        context_service=runtime.context_service,
        vocabulary=ActiveCorpusVocabulary(),
        max_subqueries=3,
    )

    plan = result["rewrite_plan"]
    # 矛盾计划被拒绝后走保守降级：不检索、无子查询、可展示理由。
    assert plan["need_retrieval"] is False
    assert plan["retrieval_decision"]["decision"] == "skip"
    assert plan["retrieval_decision"]["basis_codes"] == ["PLANNER_UNAVAILABLE"]
    assert "一致性校验" in plan["retrieval_decision"]["rationale"]
    assert plan["subqueries"] == []
    assert result["degraded_flags"] == ["rewrite_plan_contract_invalid"]
    assert result["executed_query_fingerprints"] == []


async def test_rewrite_node_propagates_gateway_impl_error() -> None:
    """网关自身异常（非模型不可用）不得被当作契约不一致而静默吞掉。"""
    runtime = build_runtime(openai_gateway=BadGatewayImpl())
    state = {
        "snapshot": {
            "snapshot_id": "test-snapshot",
            "current_message": "勾股定理是什么？",
            "recent_messages": [],
            "conversation_summary": None,
        },
        "plan_revision": 0,
        "executed_query_fingerprints": [],
    }

    with pytest.raises(RuntimeError, match="gateway 内部实现错误"):
        await rewrite_and_plan(
            state,
            runtime=runtime,
            context_service=runtime.context_service,
            vocabulary=ActiveCorpusVocabulary(),
            max_subqueries=3,
        )


class ReplanSkipGateway:
    """补检索轮模型改判 skip（与上一轮证据缺口矛盾）。"""

    async def rewrite_and_plan(
        self, *, context_view: dict[str, Any], prior_attempts: int
    ) -> dict[str, Any]:
        return {
            "plan_revision": 0,
            "standalone_question": "根值判别法的适用条件是什么？",
            "answer_mode": "direct",
            "need_retrieval": False,
            "retrieval_decision": {
                "decision": "skip",
                "basis_codes": ["CONVERSATION_CONTEXT_SUFFICIENT"],
                "rationale": "当前上下文足以直接回答。",
            },
            "subqueries": [],
        }


async def test_replan_round_forces_retrieve_on_skip_redecision() -> None:
    """补检索轮模型改判 skip 时，服务端按缺失方面强制 retrieve。"""
    runtime = build_runtime(openai_gateway=ReplanSkipGateway())
    state = {
        "snapshot": {
            "snapshot_id": "test-snapshot",
            "current_message": "根值判别法的适用条件是什么？",
            "recent_messages": [],
            "conversation_summary": None,
        },
        "plan_revision": 0,
        "executed_query_fingerprints": [],
        "evidence_assessment": {
            "status": "needs_more",
            "missing_aspects": ["根值判别法临界情形", "比值判别法边界"],
        },
    }

    result = await rewrite_and_plan(
        state,
        runtime=runtime,
        context_service=runtime.context_service,
        vocabulary=ActiveCorpusVocabulary(),
        max_subqueries=3,
    )

    plan = result["rewrite_plan"]
    assert plan["need_retrieval"] is True
    assert plan["answer_mode"] == "rag"
    assert plan["retrieval_decision"]["decision"] == "retrieve"
    assert [sq["query_text"] for sq in plan["subqueries"]] == [
        "根值判别法临界情形",
        "比值判别法边界",
    ]
    assert "replan_forced_retrieve" in plan["reason_codes"]
    assert "补检索轮约束" in plan["retrieval_decision"]["rationale"]
