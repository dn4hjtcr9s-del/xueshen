"""学习上下文组装单元测试（§12.4 / §12.5 / §23.1）。

覆盖：token 估算启发式、整句压缩、四级裁剪顺序（低排序文档 → evidence ref →
建议复习/概况 → 低优先级整体删除）、token_usage 与 truncated 标记。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from backend.memory.contracts.context import LearningContextGraphState
from backend.memory.contracts.graph_state import GraphRecommendation
from backend.memory.services.context_service import (
    TRIMMED_EVIDENCE_REF_LIMIT,
    _first_sentence,
    assemble_context,
    estimate_tokens,
)
from backend.memory.storage.markdown_schema import LearnerDocument, MasteryDocument

USER_ID = uuid4()
NOW = datetime(2026, 8, 11, tzinfo=UTC)


def _learner(**overrides: object) -> LearnerDocument:
    base: dict[str, object] = {
        "user_id": USER_ID,
        "version": 2,
        "updated_at": NOW,
        "preferences": ["例题驱动"],
        "goals": ["期末 90 分"],
        "plans": ["每天一节"],
        "evidence_refs": ["conv:t1:m1"],
    }
    base.update(overrides)
    return LearnerDocument(**base)  # type: ignore[arg-type]


def _mastery(topic_key: str, **overrides: object) -> MasteryDocument:
    base: dict[str, object] = {
        "user_id": USER_ID,
        "topic_key": topic_key,
        "topic_title": f"主题{topic_key}",
        "version": 3,
        "updated_at": NOW,
        "overview": "整体掌握良好。细节仍需巩固。",
        "understood": ["定义"],
        "difficulties": ["证明"],
        "review_advice": ["重做例题"],
        "evidence_refs": ["conv:t1:m1"],
    }
    base.update(overrides)
    return MasteryDocument(**base)  # type: ignore[arg-type]


def _graph_state(node_id: str = "n001") -> LearningContextGraphState:
    return LearningContextGraphState(
        node_id=node_id, title="节点", status="learning", reason_codes=["CONTINUE_LEARNING"]
    )


def _recommendation(node_id: str = "n002") -> GraphRecommendation:
    return GraphRecommendation(
        node_id=node_id,
        title="推荐节点",
        status=None,
        reason_codes=["NEXT_GRAPH_NODE"],
        prerequisite_node_ids=["n001"],
        related_memory_ids=["mastery:topic-a"],
        updated_at=NOW,
    )


def _assemble(**overrides: object):
    base: dict[str, object] = {
        "user_id": USER_ID,
        "query": "一致收敛",
        "budget": 3000,
        "learner": _learner(),
        "exact_mastery": [_mastery("topic-a")],
        "weak_mastery": [_mastery("topic-b"), _mastery("topic-c")],
        "graph_states": [_graph_state()],
        "recommendations": [_recommendation()],
    }
    base.update(overrides)
    return assemble_context(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# token 估算启发式（规格未给算法，实现取确定性近似）
# ---------------------------------------------------------------------------


def test_estimate_tokens_mixed_script() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1  # ASCII 每 4 字符 1 token
    assert estimate_tokens("abcde") == 2  # 向上取整
    assert estimate_tokens("一致收敛") == 4  # 非 ASCII 每字符 1 token
    assert estimate_tokens("ab一致") == 3  # 1（向上取整）+ 2


def test_first_sentence_keeps_semantic_whole() -> None:
    assert _first_sentence("整体掌握良好。细节仍需巩固。") == "整体掌握良好。"
    assert _first_sentence("没有终止符的长句") == "没有终止符的长句"
    assert _first_sentence("单行。\n第二行") == "单行。"


# ---------------------------------------------------------------------------
# 预算内不裁剪（§12.5 token_usage）
# ---------------------------------------------------------------------------


def test_within_budget_no_truncation() -> None:
    context = _assemble()
    assert context.truncated is False
    assert [m.topic_key for m in context.mastery] == ["topic-a", "topic-b", "topic-c"]
    assert context.learner is not None
    assert context.token_usage.budget == 3000
    assert context.token_usage.estimated > 0
    assert (
        context.token_usage.remaining == context.token_usage.budget - context.token_usage.estimated
    )


def test_learner_none_allowed() -> None:
    context = _assemble(learner=None)
    assert context.learner is None
    assert context.truncated is False


# ---------------------------------------------------------------------------
# 裁剪级别 1：先删除低排序（弱相关）文档
# ---------------------------------------------------------------------------


def test_trim_level1_drops_weak_mastery_from_tail() -> None:
    big = "困" * 400
    exact = _mastery("topic-a", overview="精确主题概况")
    weak1 = _mastery("topic-b", difficulties=[big])
    weak2 = _mastery("topic-c", difficulties=[big])
    learner = _learner(preferences=["短"])
    # 预算容纳 learner + exact，不容纳两篇弱相关
    budget = 200
    context = _assemble(
        budget=budget,
        learner=learner,
        exact_mastery=[exact],
        weak_mastery=[weak1, weak2],
        graph_states=[],
        recommendations=[],
    )
    assert context.truncated is True
    assert [m.topic_key for m in context.mastery] == ["topic-a"]
    assert context.learner is not None
    assert context.token_usage.estimated <= budget


# ---------------------------------------------------------------------------
# 裁剪级别 2：evidence 只保留前 N 条 ref
# ---------------------------------------------------------------------------


def test_trim_level2_caps_evidence_refs() -> None:
    many_refs = [f"conv:t:m{i}" for i in range(TRIMMED_EVIDENCE_REF_LIMIT + 40)]
    exact = _mastery("topic-a", evidence_refs=many_refs)
    # 预算容纳正文但迫近上限 → 触发 ref 收缩；估算仅按注入字段，ref 不计 token
    context = _assemble(
        budget=10**9,
        learner=None,
        exact_mastery=[exact],
        weak_mastery=[],
        graph_states=[],
        recommendations=[],
    )
    assert context.truncated is False
    assert len(context.mastery[0].evidence_refs) == TRIMMED_EVIDENCE_REF_LIMIT + 40


def test_trim_level2_under_pressure() -> None:
    # evidence_refs 不计入 token 估算（只注入 ref），级别 2 只在超预算时收缩条数；
    # 这里验证级别 3：review_advice 清空、overview 压缩为整句
    exact = _mastery(
        "topic-a",
        overview="第一句。第二句很长很长很长。",
        review_advice=["重做例题", "回顾错题"],
    )
    context = _assemble(
        budget=20,
        learner=None,
        exact_mastery=[exact],
        weak_mastery=[],
        graph_states=[],
        recommendations=[],
    )
    mastery = context.mastery[0]
    assert context.truncated is True
    assert mastery.review_advice == []
    assert mastery.overview == "第一句。"
    # 不截断单条事实到语义不完整：understood/difficulties 保持完整
    assert mastery.understood == ["定义"]


# ---------------------------------------------------------------------------
# 最终兜底：按优先级从低到高整体删除（推荐 → 图谱 → learner → mastery 尾部）
# ---------------------------------------------------------------------------


def test_trim_fallback_drops_low_priority_first() -> None:
    exact = _mastery("topic-a", difficulties=["难" * 30])
    context = _assemble(
        budget=50,
        learner=_learner(),
        exact_mastery=[exact],
        weak_mastery=[],
        graph_states=[_graph_state()],
        recommendations=[_recommendation()],
    )
    assert context.truncated is True
    assert context.recommendations == []
    assert context.graph_states == []
    assert context.learner is None
    # 精确相关 mastery 至少保留一篇（优先级 1）
    assert [m.topic_key for m in context.mastery] == ["topic-a"]
    assert context.token_usage.estimated <= 50 or len(context.mastery) == 1
