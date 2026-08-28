"""回答合同、局部拒答和 task-role 预算分配测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from backend.conversation.contracts.graph import (
    SnapshotBudgets,
    SnapshotMemory,
    SnapshotMessage,
    TurnContextSnapshot,
)
from backend.conversation.services.answer_context import build_answer_contract
from backend.conversation.services.token_counter import TokenCounter, WhitespaceTokenizer


def _snapshot() -> TurnContextSnapshot:
    return TurnContextSnapshot(
        snapshot_id="snapshot-1",
        created_at=datetime.now(UTC),
        current_message="比较根值判别法和比值判别法",
        recent_messages=[
            SnapshotMessage(
                message_id=uuid4(),
                role="user",
                sequence=1,
                content="我之前不会处理边界等于 1 的情况",
            )
        ],
        conversation_summary="用户正在复习级数判别法。",
        memory=SnapshotMemory(
            status="available",
            learner={"preferences": ["喜欢先看定义"]},
            mastery=[{"topic_title": "级数", "difficulties": ["边界情况"]}],
        ),
        budgets=SnapshotBudgets(retrieval_tokens=8),
    )


def _item(
    *,
    evidence_id: str,
    citation_id: str,
    subquery_id: str,
    role: str,
    content: str,
    score: float,
) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "chunk_ids": [f"chunk-{evidence_id}"],
        "matched_subquery_ids": [subquery_id],
        "content_role": role,
        "content_text": content,
        "token_count": len(content.split()),
        "score": score,
        "citation": {"citation_id": citation_id},
    }


def test_answer_contract_combines_question_history_memory_and_task_links() -> None:
    rewrite_plan = {
        "standalone_question": "比较根值判别法和比值判别法",
        "subqueries": [
            {
                "subquery_id": "sq-root",
                "query_text": "根值判别法的定义是什么",
                "intent": "definition",
                "coverage_target": "根值判别法定义",
            },
            {
                "subquery_id": "sq-ratio",
                "query_text": "比值判别法的边界情况是什么",
                "intent": "comparison",
                "coverage_target": "比值判别法边界",
            },
        ],
    }
    evidence = [
        _item(
            evidence_id="e-root",
            citation_id="C000000000001",
            subquery_id="sq-root",
            role="definition",
            content="根值 判别法 定义 正文",
            score=0.95,
        )
    ]

    contract, summary, refs = build_answer_contract(
        current_question="比较根值判别法和比值判别法",
        standalone_question=rewrite_plan["standalone_question"],
        rewrite_plan=rewrite_plan,
        snapshot=_snapshot(),
        evidence_items=evidence,
        evidence_assessment={
            "status": "insufficient",
            "missing_aspects": ["比值判别法边界"],
        },
        token_counter=TokenCounter(WhitespaceTokenizer()),
        total_budget=8,
    )

    assert contract.current_question == "比较根值判别法和比值判别法"
    assert contract.necessary_history[0].role == "summary"
    assert contract.relevant_memory.mastery[0]["topic_title"] == "级数"
    assert contract.tasks[0].evidence_ids == ["e-root"]
    assert contract.tasks[0].evidence_roles == ["definition_theorem"]
    assert contract.tasks[1].status == "missing"
    assert "sq-ratio" in " ".join(contract.partial_refusal_rules)
    assert contract.evidence_annotations[0].task_ids == ["sq-root"]
    assert "根值判别法的定义是什么" in contract.evidence_annotations[0].relevance_notes[0]
    assert "C000000000001" in summary
    assert refs == ["C000000000001"]


def test_evidence_budget_reserves_space_for_each_task_and_role() -> None:
    rewrite_plan = {
        "standalone_question": "分别解释定义和例题",
        "subqueries": [
            {
                "subquery_id": "sq-definition",
                "query_text": "定义",
                "intent": "definition",
                "coverage_target": "定义",
            },
            {
                "subquery_id": "sq-example",
                "query_text": "例题",
                "intent": "example",
                "coverage_target": "例题",
            },
        ],
    }
    evidence = [
        _item(
            evidence_id="e-definition",
            citation_id="C000000000001",
            subquery_id="sq-definition",
            role="definition",
            content="定义 一 二 三 四 五 六 七",
            score=0.99,
        ),
        _item(
            evidence_id="e-example",
            citation_id="C000000000002",
            subquery_id="sq-example",
            role="example",
            content="例题 一 二 三 四 五 六 七",
            score=0.50,
        ),
    ]

    contract, _summary, refs = build_answer_contract(
        current_question="分别解释定义和例题",
        standalone_question=rewrite_plan["standalone_question"],
        rewrite_plan=rewrite_plan,
        snapshot=_snapshot(),
        evidence_items=evidence,
        evidence_assessment={"status": "sufficient", "missing_aspects": []},
        token_counter=TokenCounter(WhitespaceTokenizer()),
        total_budget=8,
    )

    assert set(refs) == {"C000000000001", "C000000000002"}
    assert contract.evidence_budget.used_tokens <= 8
    assert contract.evidence_budget.task_budgets == {
        "sq-definition": 4,
        "sq-example": 4,
    }
    assert contract.evidence_budget.role_budgets == {
        "sq-definition": {"definition_theorem": 4},
        "sq-example": {"example_solution": 4},
    }
    assert all(task.status == "partially_covered" for task in contract.tasks)
    assert all(annotation.coverage == "partial" for annotation in contract.evidence_annotations)


def test_direct_answer_without_subqueries_uses_main_task() -> None:
    evidence = [
        _item(
            evidence_id="e-main",
            citation_id="C000000000003",
            subquery_id="unused",
            role="context",
            content="补充 上下文",
            score=0.8,
        )
    ]

    contract, _summary, _refs = build_answer_contract(
        current_question="继续",
        standalone_question="继续解释",
        rewrite_plan={"standalone_question": "继续解释", "subqueries": []},
        snapshot=_snapshot(),
        evidence_items=evidence,
        evidence_assessment=None,
        token_counter=TokenCounter(WhitespaceTokenizer()),
        total_budget=8,
    )

    assert contract.tasks[0].task_id == "task-main"
    assert contract.tasks[0].evidence_ids == ["e-main"]
    assert contract.evidence_annotations[0].task_ids == ["task-main"]
    assert contract.evidence_annotations[0].relevance_notes == [
        "task-main: 未拆分子问题，chunk 归入当前主任务"
    ]


def test_direct_answer_without_retrieval_does_not_trigger_refusal() -> None:
    contract, summary, refs = build_answer_contract(
        current_question="你好",
        standalone_question="你好",
        rewrite_plan={
            "standalone_question": "你好",
            "need_retrieval": False,
            "subqueries": [],
        },
        snapshot=_snapshot(),
        evidence_items=[],
        evidence_assessment=None,
        token_counter=TokenCounter(WhitespaceTokenizer()),
        total_budget=8,
    )

    assert contract.tasks[0].required is False
    assert contract.tasks[0].status == "covered"
    assert contract.partial_refusal_rules == ["证据支持的任务正常回答，不得把长期记忆当教材证据。"]
    assert summary == "（无证据）"
    assert refs == []
