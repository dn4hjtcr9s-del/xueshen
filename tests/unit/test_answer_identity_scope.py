"""ANSWER 提示词 IDENTITY 段与 CORPUS_SCOPE 注入单元测试。

覆盖（docs/answer-agent-prompt-design-proposal.md §1）：
- ANSWER_SYSTEM_PROMPT 含 xueshen 身份与 CORPUS_SCOPE 口径，且证据纪律不回流失；
- build_corpus_scope_view 的导出与章节前缀截断不变量（不静默丢内容）；
- build_answer_view 的 corpus_scope 缺省与注入。
"""

from __future__ import annotations

from backend.conversation.contracts.graph import SnapshotMemory, TurnContextSnapshot
from backend.conversation.contracts.retrieval import ActiveCorpusVocabulary
from backend.conversation.graph.prompts import ANSWER_SYSTEM_PROMPT
from backend.conversation.services.context_service import ContextService
from backend.conversation.services.corpus_vocabulary import (
    CORPUS_SCOPE_CHAPTER_PREFIXES_MAX,
    build_corpus_scope_view,
)
from backend.conversation.services.token_counter import TokenCounter
from backend.settings import Settings


def test_answer_prompt_carries_xueshen_identity() -> None:
    """IDENTITY 段：xueshen 身份、口吻与 CORPUS_SCOPE 口径都在提示词里。"""
    assert "你是 xueshen" in ANSWER_SYSTEM_PROMPT
    assert "IDENTITY" in ANSWER_SYSTEM_PROMPT
    assert "CORPUS_SCOPE" in ANSWER_SYSTEM_PROMPT
    assert "不得凭训练知识自行列举书目或知识点" in ANSWER_SYSTEM_PROMPT


def test_answer_prompt_keeps_evidence_discipline() -> None:
    """既有证据/引用纪律不回流失（抽查关键规则原文）。"""
    assert "只能引用证据集中提供的 Citation" in ANSWER_SYSTEM_PROMPT
    assert "无 RAG 证据的问题不得伪造教材引用" in ANSWER_SYSTEM_PROMPT
    assert "教材检索暂不可用，尚未从教材核实" in ANSWER_SYSTEM_PROMPT


def test_corpus_scope_view_empty_when_no_active_corpus() -> None:
    scope = build_corpus_scope_view(ActiveCorpusVocabulary())

    assert scope["available"] is False
    assert scope["book_ids"] == []
    assert scope["grade_levels"] == []
    assert scope["chapter_prefixes"] == []
    assert scope["chapter_prefixes_truncated"] is False
    assert scope["chapter_count"] == 0


def test_corpus_scope_view_exports_vocabulary() -> None:
    vocabulary = ActiveCorpusVocabulary(
        allowed_book_ids=("01_fudan_shufen_textbook_1", "02_fudan_shufen_textbook_2"),
        allowed_grade_levels=("大学数学",),
        allowed_chapter_prefixes=("第一章 集合与映射", "第二章 实数理论"),
    )
    scope = build_corpus_scope_view(vocabulary)

    assert scope["available"] is True
    assert scope["version"] == vocabulary.version
    assert scope["book_ids"] == ["01_fudan_shufen_textbook_1", "02_fudan_shufen_textbook_2"]
    assert scope["grade_levels"] == ["大学数学"]
    assert scope["chapter_prefixes"] == ["第一章 集合与映射", "第二章 实数理论"]
    assert scope["chapter_prefixes_truncated"] is False
    assert scope["chapter_count"] == 2


def test_corpus_scope_view_caps_chapter_prefixes() -> None:
    """章节前缀超上限时截断并显式置标记，不静默丢内容。"""
    total = CORPUS_SCOPE_CHAPTER_PREFIXES_MAX + 10
    vocabulary = ActiveCorpusVocabulary(
        allowed_book_ids=("book-1",),
        allowed_chapter_prefixes=tuple(f"第{i}章" for i in range(total)),
    )
    scope = build_corpus_scope_view(vocabulary)

    assert len(scope["chapter_prefixes"]) == CORPUS_SCOPE_CHAPTER_PREFIXES_MAX
    assert scope["chapter_prefixes_truncated"] is True
    assert scope["chapter_count"] == total


def _service() -> ContextService:
    return ContextService(settings=Settings(app_env="test"), token_counter=TokenCounter())


def _snapshot() -> TurnContextSnapshot:
    return TurnContextSnapshot(
        snapshot_id="s-1",
        current_message="你能教我什么？",
        memory=SnapshotMemory(status="unavailable"),
    )


def test_answer_view_defaults_corpus_scope_to_empty() -> None:
    """未注入时 corpus_scope 为空 dict，提示词按"语料暂未装配"口径回答。"""
    view = _service().build_answer_view(
        snapshot=_snapshot(),
        standalone_question="你能教我什么",
        evidence_summary="",
        evidence_refs=[],
        degraded_flags=[],
    )

    assert view["corpus_scope"] == {}


def test_answer_view_injects_corpus_scope() -> None:
    scope = build_corpus_scope_view(
        ActiveCorpusVocabulary(
            allowed_book_ids=("01_fudan_shufen_textbook_1",),
            allowed_grade_levels=("大学数学",),
        )
    )
    view = _service().build_answer_view(
        snapshot=_snapshot(),
        standalone_question="你能教我什么",
        evidence_summary="",
        evidence_refs=[],
        degraded_flags=[],
        corpus_scope=scope,
    )

    assert view["corpus_scope"]["available"] is True
    assert view["corpus_scope"]["book_ids"] == ["01_fudan_shufen_textbook_1"]
