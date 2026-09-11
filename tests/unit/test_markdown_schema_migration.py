"""文档 schema v1→v2 机械迁移的单元测试（memory-rebuild §5.6 Phase 4）。

测的是迁移的**核心决策逻辑** ``_upgrade_to_schema_v2``（纯函数，无 IO）：
- v1 → v2 的字段投影规则（name 取既有标题、description 取首行、links 现算）；
- 已是 v2 时返回 None → 上层据此跳过，**不产生新版本**（幂等的关键）；
- 坏文档抛 MarkdownParseError → 上层记入 failures 而不阻塞同批其他用户。

真实库上的"二次迁移无新版本 / 历史 versions 字节级不变"需要 DB 集成测试，
尚未补（见 PHASE4-HANDOFF.md 第 8 项）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from backend.memory.graph.maintenance import _upgrade_to_schema_v2
from backend.memory.storage.markdown_schema import (
    SCHEMA_VERSION_V1,
    SCHEMA_VERSION_V2,
    LearnerDocument,
    MarkdownParseError,
    MasteryDocument,
    parse_learner,
    parse_mastery,
    render_learner,
    render_mastery,
)

_USER = uuid.UUID("11111111-1111-4111-8111-111111111111")
_NOW = datetime(2026, 9, 10, 5, 0, 0, tzinfo=UTC)


def _v1_learner_text() -> str:
    return render_learner(
        LearnerDocument(
            user_id=_USER,
            version=1,
            updated_at=_NOW,
            schema_version=SCHEMA_VERSION_V1,
            goals=["掌握线性代数"],
            preferences=["偏好推导式讲解"],
            plans=["本周完成 [[mastery:导数]]"],
        )
    )


def _v1_mastery_text() -> str:
    return render_mastery(
        MasteryDocument(
            user_id=_USER,
            topic_key="椭圆",
            topic_title="椭圆",
            version=2,
            updated_at=_NOW,
            schema_version=SCHEMA_VERSION_V1,
            overview="与 [[抛物线]] 的焦点性质混淆",
            understood=["掌握第一定义"],
        )
    )


# ---------------------------------------------------------------------------
# v1 → v2
# ---------------------------------------------------------------------------


def test_learner_upgrade_derives_name_and_description() -> None:
    upgraded = _upgrade_to_schema_v2(memory_type="learner", text=_v1_learner_text())
    assert upgraded is not None
    doc = parse_learner(upgraded)
    assert doc.schema_version == SCHEMA_VERSION_V2
    assert doc.name == "学习者档案"
    assert doc.description == "掌握线性代数", "description 取目标/偏好的首条"
    assert doc.links == ["mastery:导数"], "links 从正文现算"


def test_mastery_upgrade_derives_name_from_topic_title() -> None:
    upgraded = _upgrade_to_schema_v2(memory_type="mastery", text=_v1_mastery_text())
    assert upgraded is not None
    doc = parse_mastery(upgraded)
    assert doc.schema_version == SCHEMA_VERSION_V2
    assert doc.name == "椭圆"
    assert doc.description == "与 [[抛物线]] 的焦点性质混淆"
    assert doc.links == ["抛物线"]
    assert doc.aliases == [], "迁移不编造别名，留给 planner 用 frontmatter_patch 补"


def test_upgrade_preserves_body_content() -> None:
    """迁移只动 frontmatter，正文事实一条不能丢。"""
    upgraded = _upgrade_to_schema_v2(memory_type="mastery", text=_v1_mastery_text())
    assert upgraded is not None
    doc = parse_mastery(upgraded)
    assert doc.understood == ["掌握第一定义"]
    assert doc.overview == "与 [[抛物线]] 的焦点性质混淆"


def test_upgrade_output_is_single_line_description() -> None:
    """description 必须单行，否则迁移会产出自己解析不了的文档。"""
    text = render_mastery(
        MasteryDocument(
            user_id=_USER,
            topic_key="长主题",
            topic_title="长主题",
            version=1,
            updated_at=_NOW,
            schema_version=SCHEMA_VERSION_V1,
            overview="第一行内容\n第二行内容",
        )
    )
    upgraded = _upgrade_to_schema_v2(memory_type="mastery", text=text)
    assert upgraded is not None
    assert "\n" not in parse_mastery(upgraded).description


# ---------------------------------------------------------------------------
# 幂等：已是 v2 就不再升级
# ---------------------------------------------------------------------------


def test_already_v2_mastery_returns_none() -> None:
    v2_text = render_mastery(
        MasteryDocument(
            user_id=_USER,
            topic_key="椭圆",
            topic_title="椭圆",
            version=3,
            updated_at=_NOW,
            schema_version=SCHEMA_VERSION_V2,
            name="椭圆",
            description="圆锥曲线之一",
        )
    )
    assert _upgrade_to_schema_v2(memory_type="mastery", text=v2_text) is None


def test_already_v2_learner_returns_none() -> None:
    v2_text = render_learner(
        LearnerDocument(
            user_id=_USER,
            version=3,
            updated_at=_NOW,
            schema_version=SCHEMA_VERSION_V2,
            name="学习者档案",
            description="偏好与目标",
        )
    )
    assert _upgrade_to_schema_v2(memory_type="learner", text=v2_text) is None


def test_upgrade_is_stable_when_applied_twice() -> None:
    """升级结果再升级必须无变化——否则二次迁移会产生无意义的新版本。"""
    once = _upgrade_to_schema_v2(memory_type="mastery", text=_v1_mastery_text())
    assert once is not None
    assert _upgrade_to_schema_v2(memory_type="mastery", text=once) is None


# ---------------------------------------------------------------------------
# 坏文档与未知类型
# ---------------------------------------------------------------------------


def test_broken_document_raises_for_caller_to_record_failure() -> None:
    with pytest.raises(MarkdownParseError):
        _upgrade_to_schema_v2(memory_type="mastery", text="完全不是 markdown 文档")


def test_wrong_kind_raises() -> None:
    """把 learner 文本当 mastery 迁移必须报错，而不是产出畸形文档。"""
    with pytest.raises(MarkdownParseError):
        _upgrade_to_schema_v2(memory_type="mastery", text=_v1_learner_text())


def test_index_type_is_not_migrated_here() -> None:
    """index 由 rebuild_index 重新生成，本函数对它返回 None（上层也跳过）。"""
    assert _upgrade_to_schema_v2(memory_type="index", text="任意") is None
