"""keywords 全链路（frontmatter v2 / FrontMatterPatch / index 块）单元测试。

对应 memory-rebuild §2.3 / §3.4 与用户 2026-09-12 裁决 A：**keywords 是 v2 frontmatter
字段，文档是唯一事实源**，index.md 与 PG 索引列都只是它的投影。

硬约束（§5.6）：v1 历史文件读进来再渲染必须仍是 v1，且**不得出现 keywords 字段**。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.memory.contracts.commands import FrontMatterPatch
from backend.memory.services.memory_service import apply_frontmatter_patch
from backend.memory.storage.markdown_schema import (
    SCHEMA_VERSION_V1,
    SCHEMA_VERSION_V2,
    IndexDocument,
    IndexEntry,
    LearnerDocument,
    MasteryDocument,
    parse_index,
    parse_learner,
    parse_mastery,
    render_index,
    render_learner,
    render_mastery,
)

_USER = uuid.UUID("22222222-2222-4222-8222-222222222222")
_NOW = datetime(2026, 9, 12, 5, 0, 0, tzinfo=UTC)


def _learner(**overrides: object) -> LearnerDocument:
    base: dict[str, object] = {
        "user_id": _USER,
        "version": 2,
        "updated_at": _NOW,
        "preferences": ["偏好推导式讲解"],
        "goals": ["掌握线性代数"],
    }
    base.update(overrides)
    return LearnerDocument(**base)  # type: ignore[arg-type]


def _mastery(**overrides: object) -> MasteryDocument:
    base: dict[str, object] = {
        "user_id": _USER,
        "topic_key": "椭圆",
        "topic_title": "椭圆",
        "version": 3,
        "updated_at": _NOW,
        "overview": "与 [[抛物线]] 的焦点性质混淆",
        "understood": ["掌握第一定义"],
    }
    base.update(overrides)
    return MasteryDocument(**base)  # type: ignore[arg-type]


def _v2_mastery(**overrides: object) -> MasteryDocument:
    return _mastery(
        schema_version=SCHEMA_VERSION_V2,
        name="椭圆",
        description="圆锥曲线之一",
        **overrides,
    )


# ---------------------------------------------------------------------------
# 默认值与 v2 往返
# ---------------------------------------------------------------------------


def test_keywords_default_to_empty_list_on_both_documents() -> None:
    assert _mastery().keywords == []
    assert _learner().keywords == []


def test_v2_mastery_keywords_round_trip_with_cjk_and_special_chars() -> None:
    """中文、等号/尖角号、双引号都必须原样往返（frontmatter 走 JSON 数组）。"""
    keywords = ["椭圆", "焦点/准线", "x^2/a^2+y^2/b^2=1", '常见错误："长轴"与"短轴"混淆']
    doc = _v2_mastery(keywords=keywords)
    text = render_mastery(doc)
    assert "keywords:" in text
    assert parse_mastery(text).keywords == keywords


def test_v2_learner_keywords_round_trip() -> None:
    doc = _learner(
        schema_version=SCHEMA_VERSION_V2,
        name="学习者档案",
        description="偏好与目标",
        keywords=["推导式讲解", "代数基础"],
    )
    back = parse_learner(render_learner(doc))
    assert back.keywords == ["推导式讲解", "代数基础"]
    assert back.name == "学习者档案", "keywords 不得挤掉其它 v2 字段"


def test_v2_empty_keywords_render_deterministically() -> None:
    """空列表也渲染成 ``keywords: []``：省略字段会让"没关键词"与"没升级"无法区分。"""
    text = render_mastery(_v2_mastery())
    assert "keywords: []" in text
    assert parse_mastery(text).keywords == []
    assert render_mastery(_v2_mastery()) == text


def test_v2_document_without_keywords_field_still_parses() -> None:
    """兼容本字段上线前写出的 v2 文件（没有 keywords 行）。"""
    text = render_mastery(_v2_mastery(keywords=["椭圆"]))
    stripped = "\n".join(line for line in text.split("\n") if not line.startswith("keywords:"))
    assert "keywords:" not in stripped
    assert parse_mastery(stripped).keywords == []


def test_keywords_are_normalized_on_parse() -> None:
    """去首尾空白 / 丢空串 / 去重保序，与 aliases 同款语义。"""
    doc = _v2_mastery(keywords=[" 椭圆 ", "", "椭圆", "焦点"])
    assert parse_mastery(render_mastery(doc)).keywords == ["椭圆", "焦点"]


def test_handwritten_pipe_keywords_are_accepted() -> None:
    """兼容手写 ``a | b``（与 aliases 相同的宽容读入规则）。"""
    text = render_mastery(_v2_mastery())
    patched = text.replace("keywords: []", 'keywords: "焦点 | 判别式"')
    assert parse_mastery(patched).keywords == ["焦点", "判别式"]


# ---------------------------------------------------------------------------
# v1 硬约束（§5.6）
# ---------------------------------------------------------------------------


def test_v1_mastery_never_renders_keywords() -> None:
    """即使对象被硬塞了 keywords，v1 也不能把它写进文件（更不得升版）。"""
    doc = _mastery(schema_version=SCHEMA_VERSION_V1, keywords=["椭圆", "焦点"])
    text = render_mastery(doc)
    assert "keywords" not in text
    assert "schema_version: 1" in text
    back = parse_mastery(text)
    assert back.schema_version == SCHEMA_VERSION_V1
    assert back.keywords == []


def test_v1_learner_never_renders_keywords() -> None:
    doc = _learner(schema_version=SCHEMA_VERSION_V1, keywords=["代数基础"])
    text = render_learner(doc)
    assert "keywords" not in text
    assert parse_learner(text).keywords == []


# ---------------------------------------------------------------------------
# FrontMatterPatch 合并语义
# ---------------------------------------------------------------------------


def test_frontmatter_patch_keywords_append_dedupe_and_keep_order() -> None:
    doc = _v2_mastery(keywords=["椭圆", "焦点"])
    apply_frontmatter_patch(doc, FrontMatterPatch(keywords=["焦点", " 判别式 ", "判别式"]))
    assert doc.keywords == ["椭圆", "焦点", "判别式"]
    assert doc.schema_version == SCHEMA_VERSION_V2


def test_frontmatter_patch_keywords_only_keeps_existing_frontmatter() -> None:
    """只补 keywords：name/description/aliases 原样保留，文档继续是 v2。"""
    doc = _v2_mastery(aliases=["ellipse"])
    apply_frontmatter_patch(doc, FrontMatterPatch(keywords=["准线"]))
    assert doc.keywords == ["准线"]
    assert (doc.name, doc.description, doc.aliases) == ("椭圆", "圆锥曲线之一", ["ellipse"])
    text = render_mastery(doc)
    assert parse_mastery(text).keywords == ["准线"]


def test_frontmatter_patch_keywords_rejects_more_than_eight() -> None:
    with pytest.raises(ValidationError):
        FrontMatterPatch(keywords=[f"k{i}" for i in range(9)])


def test_frontmatter_patch_keywords_default_is_empty() -> None:
    assert FrontMatterPatch().keywords == []


def test_keywords_only_patch_on_v1_document_stays_v1() -> None:
    """v2 解析器要求 name/description 必填：半套补丁不得把 v1 文档升成 v2。

    代价是 v1 文档上的 keywords 补丁暂时不落盘（必须先由迁移任务升到 v2）。
    """
    doc = _mastery(schema_version=SCHEMA_VERSION_V1)
    apply_frontmatter_patch(doc, FrontMatterPatch(keywords=["焦点"]))
    assert doc.schema_version == SCHEMA_VERSION_V1
    assert "keywords" not in render_mastery(doc)


# ---------------------------------------------------------------------------
# index 块往返（§3.4）
# ---------------------------------------------------------------------------


def _index_doc(entry: IndexEntry) -> IndexDocument:
    return IndexDocument(
        user_id=_USER,
        version=7,
        updated_at=_NOW,
        schema_version=SCHEMA_VERSION_V2,
        learner=IndexEntry(
            memory_id="learner",
            memory_type="learner",
            topic_key=None,
            title="学习者档案",
            version=2,
            updated_at=_NOW,
            description="偏好与目标",
            keywords=["推导式讲解"],
        ),
        mastery_entries=[entry],
    )


def test_index_entry_keywords_round_trip_through_v2_block() -> None:
    entry = IndexEntry(
        memory_id="mastery:椭圆",
        memory_type="mastery",
        topic_key="椭圆",
        title="椭圆",
        version=3,
        updated_at=_NOW,
        description="圆锥曲线之一",
        aliases=["ellipse"],
        related_topic_keys=["抛物线"],
        keywords=["焦点", "准线", "离心率 e"],
    )
    text = render_index(_index_doc(entry))
    assert "### mastery:椭圆" in text
    assert "- keywords: 焦点 | 准线 | 离心率 e" in text
    assert "- description: 圆锥曲线之一" in text
    assert "- aliases: ellipse" in text

    back = parse_index(text)
    assert back.mastery_entries[0].keywords == ["焦点", "准线", "离心率 e"]
    assert back.mastery_entries[0].description == "圆锥曲线之一"
    assert back.mastery_entries[0].aliases == ["ellipse"]
    assert back.mastery_entries[0].related_topic_keys == ["抛物线"]
    assert back.learner is not None and back.learner.keywords == ["推导式讲解"]


def test_index_entry_without_keywords_renders_empty_value() -> None:
    """无关键词时也渲染 ``- keywords:``（取值空），解析回来仍是空列表。"""
    entry = IndexEntry(
        memory_id="mastery:双曲线",
        memory_type="mastery",
        topic_key="双曲线",
        title="双曲线",
        version=1,
        updated_at=_NOW,
    )
    text = render_index(_index_doc(entry))
    assert "- keywords:" in text
    assert parse_index(text).mastery_entries[0].keywords == []
