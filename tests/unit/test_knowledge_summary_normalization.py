"""知识总结 Phase 1 规范化、哈希和来源聚合 DTO 测试。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from backend.conversation.contracts.knowledge_summary import (
    KnowledgeSummaryContent,
    KnowledgeSummaryItem,
    KnowledgeSummarySourceView,
)
from backend.conversation.knowledge_summary.normalization import (
    canonicalize_item_text_v1,
    canonicalize_quote_v1,
    canonicalize_title_v1,
    content_hash_v1,
    state_hash_v1,
)


def _content() -> KnowledgeSummaryContent:
    """构造带消息级 source_id 的最小有效 content v1。"""
    return KnowledgeSummaryContent(
        overview=KnowledgeSummaryItem(
            item_id=UUID(int=1),
            text="椭圆离心率描述椭圆扁平程度。",
            origin="ai",
            source_ids=[UUID(int=2)],
        ),
        formulas=[
            KnowledgeSummaryItem(
                item_id=UUID(int=3),
                text="椭圆离心率 e=c/a，且 0<e<1。",
                origin="ai",
                source_ids=[UUID(int=4)],
            )
        ],
    )


def test_canonicalization_follows_frozen_v1_examples() -> None:
    """标题、条目和 quote 均使用方案 §11.1 的不同规则。"""
    assert canonicalize_title_v1("《椭圆：离心率》", max_length=240) == "椭圆 离心率"
    assert canonicalize_title_v1("Ellipse  ＋  Focus", max_length=240) == "ellipse + focus"
    assert canonicalize_item_text_v1("  e=c/a。．\n") == "e=c/a."
    assert canonicalize_quote_v1("第一行\r\n\t第二行\u3000第三行") == "第一行 第二行 第三行"


def test_content_and_state_hash_are_deterministic() -> None:
    """content 固定字段顺序，保护章节排序仅影响 state hash 的固定输入。"""
    content = _content()
    digest = content_hash_v1(content)
    assert digest == content_hash_v1(content)
    state_a = state_hash_v1(
        topic_group_title="圆锥曲线",
        topic_title="椭圆的离心率",
        content_hash=digest,
        protected_sections=["methods", "definitions"],
        review_state="clean",
    )
    state_b = state_hash_v1(
        topic_group_title="圆锥曲线",
        topic_title="椭圆的离心率",
        content_hash=digest,
        protected_sections=["definitions", "methods"],
        review_state="clean",
    )
    assert state_a == state_b


def test_source_view_uses_turn_card_identifier_not_internal_source_id() -> None:
    """Phase 1 冻结：API 只暴露 source_turn_id，拒绝旧 source_id 字段。"""
    turn_id = uuid4()
    view = KnowledgeSummarySourceView(
        source_turn_id=turn_id,
        thread_id=uuid4(),
        turn_id=turn_id,
        support_message_ids=[uuid4()],
        support_roles=["assistant"],
        question_excerpt=None,
        status="unavailable",
        occurred_at=datetime(2026, 8, 17, tzinfo=UTC),
    )
    assert view.source_turn_id == turn_id
    with pytest.raises(ValidationError, match="source_id"):
        KnowledgeSummarySourceView.model_validate(
            {
                "source_id": str(uuid4()),
                "source_turn_id": str(turn_id),
                "thread_id": str(uuid4()),
                "turn_id": str(turn_id),
                "support_message_ids": [],
                "support_roles": [],
                "question_excerpt": None,
                "status": "available",
                "occurred_at": "2026-08-17T00:00:00Z",
            }
        )
