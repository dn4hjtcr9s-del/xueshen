"""Markdown schema v2 单元测试（memory-rebuild §5.6 Phase 4 / §5.11）。

对应 §5.6「迁移验收」的前半部分：v1 / v2 / 坏 front matter / 非法 link / 重复 alias /
Unicode topic key fixture 都能被解析器明确分类；且 **v1 历史文件读进来写回去仍是 v1**
（§5.6 明令不得把 v1 序列化成 v2 覆盖原文件）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from backend.memory.storage.markdown_schema import (
    CURRENT_SCHEMA_VERSION,
    SCHEMA_VERSION_V1,
    SCHEMA_VERSION_V2,
    IndexDocument,
    IndexEntry,
    LearnerDocument,
    MarkdownParseError,
    MasteryDocument,
    document_links,
    extract_links,
    normalize_aliases,
    parse_index,
    parse_learner,
    parse_mastery,
    render_index,
    render_learner,
    render_mastery,
)

_USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
_NOW = datetime(2026, 9, 10, 5, 0, 0, tzinfo=UTC)


def _learner(**overrides: object) -> LearnerDocument:
    base: dict[str, object] = {
        "user_id": _USER,
        "version": 3,
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
        "version": 5,
        "updated_at": _NOW,
        "overview": "与 [[抛物线]] 的焦点性质混淆",
        "understood": ["掌握第一定义"],
    }
    base.update(overrides)
    return MasteryDocument(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 版本常量与 v1 兼容（最关键的回归面）
# ---------------------------------------------------------------------------


def test_current_schema_version_is_v2() -> None:
    assert CURRENT_SCHEMA_VERSION == SCHEMA_VERSION_V2 == 2
    assert SCHEMA_VERSION_V1 == 1


def test_v1_learner_round_trip_stays_v1() -> None:
    """v1 文档读进来写回去必须仍是 v1——不得被静默升级。"""
    doc = _learner(schema_version=SCHEMA_VERSION_V1)
    text = render_learner(doc)
    assert "schema_version: 1" in text
    assert "name:" not in text, "v1 不应写入 v2 字段"
    back = parse_learner(text)
    assert back.schema_version == SCHEMA_VERSION_V1
    assert back.name is None
    assert back.aliases == []


def test_v1_mastery_round_trip_stays_v1() -> None:
    text = render_mastery(_mastery(schema_version=SCHEMA_VERSION_V1))
    assert "schema_version: 1" in text
    assert parse_mastery(text).schema_version == SCHEMA_VERSION_V1


def test_missing_schema_version_defaults_to_v1() -> None:
    """历史文件可能没有 schema_version 字段，必须按 v1 处理而不是报错。"""
    text = render_mastery(_mastery(schema_version=SCHEMA_VERSION_V1))
    without = "\n".join(line for line in text.split("\n") if not line.startswith("schema_version:"))
    assert parse_mastery(without).schema_version == SCHEMA_VERSION_V1


# ---------------------------------------------------------------------------
# v2 往返
# ---------------------------------------------------------------------------


def test_v2_mastery_round_trip_preserves_frontmatter_and_links() -> None:
    doc = _mastery(
        schema_version=SCHEMA_VERSION_V2,
        name="椭圆",
        description="圆锥曲线之一",
        aliases=["ellipse", "椭圆形"],
    )
    back = parse_mastery(render_mastery(doc))
    assert back.schema_version == SCHEMA_VERSION_V2
    assert back.name == "椭圆"
    assert back.description == "圆锥曲线之一"
    assert back.aliases == ["ellipse", "椭圆形"]
    assert back.links == ["抛物线"], "正文 [[link]] 必须被提取"


def test_v2_learner_round_trip() -> None:
    doc = _learner(
        schema_version=SCHEMA_VERSION_V2,
        name="学习者档案",
        description="偏好与目标",
        aliases=["profile"],
        plans=["本周完成 [[mastery:导数]]"],
    )
    back = parse_learner(render_learner(doc))
    assert back.schema_version == SCHEMA_VERSION_V2
    assert back.name == "学习者档案"
    assert back.aliases == ["profile"]
    assert back.links == ["mastery:导数"]


def test_v2_index_round_trip_with_block_entries() -> None:
    doc = IndexDocument(
        user_id=_USER,
        version=4,
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
        ),
        mastery_entries=[
            IndexEntry(
                memory_id="mastery:椭圆",
                memory_type="mastery",
                topic_key="椭圆",
                title="椭圆",
                version=3,
                updated_at=_NOW,
                description="圆锥曲线之一",
                aliases=["ellipse"],
                related_topic_keys=["抛物线"],
                keywords=["焦点"],
            )
        ],
    )
    text = render_index(doc)
    assert "schema_version: 2" in text
    assert "### mastery:椭圆" in text, "v2 index 必须是块结构"
    back = parse_index(text)
    assert back.schema_version == SCHEMA_VERSION_V2
    assert back.learner is not None and back.learner.description == "偏好与目标"
    entry = back.mastery_entries[0]
    assert (entry.aliases, entry.related_topic_keys, entry.keywords) == (
        ["ellipse"],
        ["抛物线"],
        ["焦点"],
    )


def test_v1_index_still_uses_single_line_entries() -> None:
    """v1 index 解析不能因为引入块结构而失效（双读）。"""
    doc = IndexDocument(
        user_id=_USER,
        version=1,
        updated_at=_NOW,
        schema_version=SCHEMA_VERSION_V1,
        learner=IndexEntry(
            memory_id="learner",
            memory_type="learner",
            topic_key=None,
            title="学习者档案",
            version=1,
            updated_at=_NOW,
        ),
    )
    text = render_index(doc)
    back = parse_index(text)
    assert back.schema_version == SCHEMA_VERSION_V1
    assert back.learner is not None and back.learner.title == "学习者档案"


# ---------------------------------------------------------------------------
# 校验：坏文档必须被明确拒绝，而不是静默写坏
# ---------------------------------------------------------------------------


def test_v2_missing_name_is_rejected() -> None:
    text = render_mastery(_mastery(schema_version=SCHEMA_VERSION_V2, name="椭圆", description="d"))
    broken = text.replace('name: "椭圆"\n', "")
    with pytest.raises(MarkdownParseError, match="name"):
        parse_mastery(broken)


def test_v2_missing_description_is_rejected() -> None:
    text = render_mastery(
        _mastery(schema_version=SCHEMA_VERSION_V2, name="椭圆", description="一行描述")
    )
    broken = text.replace('description: "一行描述"\n', "")
    with pytest.raises(MarkdownParseError, match="description"):
        parse_mastery(broken)


def test_v2_multiline_description_is_rejected() -> None:
    """description 要投影进注册表的一行，多行必须拒绝。"""
    text = render_mastery(
        _mastery(schema_version=SCHEMA_VERSION_V2, name="椭圆", description="第一行")
    )
    broken = text.replace('description: "第一行"', 'description: "第一行\\n第二行"')
    with pytest.raises(MarkdownParseError, match="单行"):
        parse_mastery(broken)


def test_broken_front_matter_is_classified() -> None:
    with pytest.raises(MarkdownParseError, match="front matter"):
        parse_mastery("没有 front matter 的正文")
    with pytest.raises(MarkdownParseError, match="kind"):
        parse_mastery("---\nkind: learner-profile\n---\n\n# x\n")


def test_illegal_schema_version_is_rejected() -> None:
    text = render_mastery(_mastery(schema_version=SCHEMA_VERSION_V1))
    broken = text.replace("schema_version: 1", 'schema_version: "abc"')
    with pytest.raises(MarkdownParseError, match="schema_version"):
        parse_mastery(broken)


# ---------------------------------------------------------------------------
# links / aliases 工具
# ---------------------------------------------------------------------------


def test_extract_links_dedupes_and_keeps_order() -> None:
    text = "与 [[抛物线]] 混淆；另见 [[mastery:导数]] 与 [[抛物线]]"
    assert extract_links(text) == ["抛物线", "mastery:导数"]


def test_dangling_links_are_legal() -> None:
    """§3.2：悬空链接合法，提取不报错、不做存在性校验。"""
    assert extract_links("尚无建档的 [[双曲线]]") == ["双曲线"]


def test_document_links_ignores_empty_and_whitespace() -> None:
    assert document_links([], "[[ ]]", "  ") == []


def test_normalize_aliases_dedupes_and_strips() -> None:
    assert normalize_aliases([" ellipse ", "", "椭圆", "ellipse"]) == ["ellipse", "椭圆"]


def test_normalize_aliases_does_not_fold_case_or_unicode() -> None:
    """别名是检索键，折叠会让本应区分的写法互相吞并。"""
    assert normalize_aliases(["Ellipse", "ellipse", "椭圆形", "椭圆"]) == [
        "Ellipse",
        "ellipse",
        "椭圆形",
        "椭圆",
    ]


def test_aliases_round_trip_through_json_frontmatter() -> None:
    doc = _mastery(
        schema_version=SCHEMA_VERSION_V2,
        name="椭圆",
        description="d",
        aliases=["ellipse", "椭圆形"],
    )
    text = render_mastery(doc)
    assert '["ellipse", "椭圆形"]' in text
    assert parse_mastery(text).aliases == ["ellipse", "椭圆形"]


def test_handwritten_pipe_aliases_are_accepted() -> None:
    """兼容手写 frontmatter：`a | b` 与 JSON 数组都能读。"""
    text = render_mastery(_mastery(schema_version=SCHEMA_VERSION_V2, name="椭圆", description="d"))
    patched = text.replace("aliases: []", 'aliases: "ellipse | 椭圆形"')
    assert parse_mastery(patched).aliases == ["ellipse", "椭圆形"]


# ---------------------------------------------------------------------------
# Unicode topic key
# ---------------------------------------------------------------------------


def test_unicode_topic_key_round_trips() -> None:
    doc = _mastery(
        topic_key="椭圆",
        topic_title="椭圆",
        schema_version=SCHEMA_VERSION_V2,
        name="椭圆",
        description="d",
    )
    back = parse_mastery(render_mastery(doc))
    assert back.topic_key == "椭圆"
    assert back.topic_title == "椭圆"


def test_unicode_topic_key_in_index_block() -> None:
    doc = IndexDocument(
        user_id=_USER,
        version=1,
        updated_at=_NOW,
        schema_version=SCHEMA_VERSION_V2,
        mastery_entries=[
            IndexEntry(
                memory_id="mastery:双曲线",
                memory_type="mastery",
                topic_key="双曲线",
                title="双曲线",
                version=1,
                updated_at=_NOW,
            )
        ],
    )
    back = parse_index(render_index(doc))
    assert back.mastery_entries[0].topic_key == "双曲线"
