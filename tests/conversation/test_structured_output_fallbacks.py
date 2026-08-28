"""Rewrite/Evidence 结构化输出失败时的 Graph 节点降级测试。"""

from __future__ import annotations

from typing import Any

from backend.conversation.contracts.errors import StructuredOutputError
from backend.conversation.contracts.retrieval import ActiveCorpusVocabulary
from backend.conversation.graph.nodes.evidence import evaluate_evidence
from backend.conversation.graph.nodes.rewrite import rewrite_and_plan
from tests.conversation.graph_fixtures import build_runtime


class FailingStructuredGateway:
    async def rewrite_and_plan(
        self, *, context_view: dict[str, Any], prior_attempts: int
    ) -> dict[str, Any]:
        raise _failure()

    async def assess_evidence(
        self, *, question: str, evidence_summary: str, budget_remaining: str
    ) -> dict[str, Any]:
        raise _failure()


def _failure() -> StructuredOutputError:
    return StructuredOutputError(
        "结构化输出为空",
        reason="empty_output",
        attempts=3,
        response_status="completed",
    )


async def test_rewrite_failure_falls_back_to_current_question() -> None:
    runtime = build_runtime(openai_gateway=FailingStructuredGateway())
    state = {
        "snapshot": {
            "snapshot_id": "test-snapshot",
            "current_message": "请解释根值判别法",
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

    assert result["rewrite_plan"]["standalone_question"] == "请解释根值判别法"
    assert result["rewrite_plan"]["subqueries"][0]["query_text"] == "请解释根值判别法"
    assert result["rewrite_plan"]["reason_codes"] == ["rewrite_structured_fallback"]
    assert result["degraded_flags"] == ["rewrite_structured_fallback"]


async def test_evidence_failure_with_items_falls_back_to_sufficient() -> None:
    runtime = build_runtime(openai_gateway=FailingStructuredGateway())
    state = {
        "rewrite_plan": {"standalone_question": "根值判别法是什么？"},
        "evidence_set": {
            "items": [{"content_text": "根值判别法正文", "citation": {"citation_id": "C1"}}],
            "total_tokens": 10,
        },
    }

    result = await evaluate_evidence(state, runtime=runtime)

    assert result["evidence_assessment"]["status"] == "sufficient"
    assert result["evidence_assessment"]["unsupported_claim_risk"] == "medium"
    assert result["degraded_flags"] == ["evidence_structured_fallback"]


async def test_evidence_failure_without_items_falls_back_to_insufficient() -> None:
    runtime = build_runtime(openai_gateway=FailingStructuredGateway())
    state = {
        "rewrite_plan": {"standalone_question": "根值判别法是什么？"},
        "evidence_set": {"items": [], "total_tokens": 0},
    }

    result = await evaluate_evidence(state, runtime=runtime)

    assert result["evidence_assessment"]["status"] == "insufficient"
    assert result["evidence_assessment"]["unsupported_claim_risk"] == "high"
    assert result["degraded_flags"] == ["evidence_structured_fallback"]
