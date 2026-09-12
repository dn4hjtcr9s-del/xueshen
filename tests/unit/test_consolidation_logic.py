"""consolidation 末段的纯逻辑单测（memory-rebuild §5.9①）。

图级/落盘行为（summary 版本与快照、keywords 补丁、批次幂等）由
``tests/integration/test_consolidation.py`` 用真实库与真实文件存储覆盖；这里只锁住
三件"算错了就会静默写坏数据"的纯函数：summary 渲染格式、超预算降级、旧摘要解析。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.memory.graph.consolidation import (
    PREFERENCES_HEADING,
    PROFILE_HEADING,
    ROUTES_HEADING,
    _build_payload,
    _parse_sections,
    render_summary_body,
)
from backend.memory.storage import summary_file


def _documents() -> list[dict[str, Any]]:
    return [
        {
            "memory_id": "learner",
            "memory_type": "learner",
            "version": 3,
            "preferences": ["先给结论"],
            "goals": ["高考"],
            "plans": [],
            "aliases": [],
            "links": [],
        },
        {
            "memory_id": "mastery:椭圆",
            "memory_type": "mastery",
            "topic_key": "椭圆",
            "topic_title": "椭圆",
            "name": "椭圆",
            "description": "圆锥曲线之一",
            "aliases": ["椭圆形"],
            "keywords": ["焦点", "离心率"],
            "version": 7,
            "overview": "能求标准方程",
            "understood": ["定义"],
            "difficulties": ["准线"],
            "review_advice": [],
            "links": ["[[抛物线]]"],
            "evidence_refs": ["m1"],
        },
    ]


# ---------------------------------------------------------------------------
# summary 渲染（§2.3 的 v1 格式）
# ---------------------------------------------------------------------------


def test_render_summary_body_matches_the_v1_section_layout() -> None:
    body = render_summary_body(
        user_profile=["正在系统复习圆锥曲线"],
        stable_preferences=["讲解先给结论"],
        topic_routes=[("椭圆", "能求标准方程，准线仍不稳", "learning")],
    )

    assert PROFILE_HEADING in body
    assert PREFERENCES_HEADING in body
    assert ROUTES_HEADING in body
    # 主题路由的行格式：topic_key | 一句话状态 | 熟练度 → mastery:topic_key
    assert "- 椭圆 | 能求标准方程，准线仍不稳 | learning → mastery:椭圆" in body


def test_summary_file_puts_the_schema_marker_on_the_first_line(tmp_path: Path) -> None:
    """§2.3 硬约束：首行必须恰好是 v1（prime 读取路径按它判损坏）。"""
    user_id = uuid4()
    meta = summary_file.write_summary_sync(
        tmp_path,
        user_id,
        render_summary_body(
            user_profile=[], stable_preferences=[], topic_routes=[("椭圆", "入门", "learning")]
        ),
        batch_operation_id=None,
        generated_at=datetime.now(UTC),
    )
    raw = summary_file.summary_path(tmp_path, user_id).read_text(encoding="utf-8")

    assert raw.splitlines()[0] == "v1"
    assert meta.version == 1
    assert meta.checksum


# ---------------------------------------------------------------------------
# 超预算降级（§4.5-② 决议 B 组：只重写主题路由段）
# ---------------------------------------------------------------------------


def test_payload_within_budget_is_not_degraded() -> None:
    payload, degraded = _build_payload(
        documents=_documents(),
        existing_summary="v1\n\n## 用户画像\n- 旧画像\n".encode(),
        batch_diff=[],
        max_chars=48_000,
    )

    assert degraded is False
    assert '"degraded": false' in payload
    # 预算内必须带上完整 mastery（含 overview 等正文级字段）
    assert "能求标准方程" in payload


def test_payload_over_budget_drops_to_topic_routes_only() -> None:
    payload, degraded = _build_payload(
        documents=_documents(),
        existing_summary="v1\n\n## 用户画像\n- 旧画像\n".encode(),
        batch_diff=[{"operation_id": "op-1", "outcome": "succeeded"}],
        max_chars=200,
    )

    assert degraded is True
    assert '"degraded": true' in payload
    # 降级后只留路由骨架：主题仍在
    assert "椭圆" in payload
    # 旧摘要要原样带上：降级时画像/偏好必须沿用旧版，模型得看得见它们
    assert "旧画像" in payload


# ---------------------------------------------------------------------------
# 旧摘要解析（降级时保留画像/偏好）
# ---------------------------------------------------------------------------


def test_parse_sections_reads_bullets_back() -> None:
    text = (
        "v1\n\n"
        "## 用户画像\n- 正在复习圆锥曲线\n- 偏好先给结论\n\n"
        "## 稳定偏好\n- 讲解先给结论\n\n"
        "## 主题路由\n- 椭圆 | 入门 | learning → mastery:椭圆\n"
    )
    sections = _parse_sections(text)

    assert sections[PROFILE_HEADING] == ["正在复习圆锥曲线", "偏好先给结论"]
    assert sections[PREFERENCES_HEADING] == ["讲解先给结论"]
    # 路由段也会被解析出来（当前只在降级时用画像/偏好，路由按新结果重写）
    assert sections[ROUTES_HEADING] == ["椭圆 | 入门 | learning → mastery:椭圆"]


def test_parse_sections_ignores_unknown_headings() -> None:
    sections = _parse_sections("v1\n\n## 别的段\n- 不该被读到\n\n## 用户画像\n- A\n")

    assert sections[PROFILE_HEADING] == ["A"]
    assert "## 别的段" not in sections


# ---------------------------------------------------------------------------
# §5.9④ 候选主题区（index v2）
# ---------------------------------------------------------------------------


def test_index_v2_renders_and_parses_candidate_topics() -> None:
    """候选主题区必须能往返：index 是投影，重建后不能丢"还没到门槛的悬空链接"。"""
    from uuid import uuid4

    from backend.memory.storage.markdown_schema import (
        IndexDocument,
        parse_index,
        render_index,
    )

    doc = IndexDocument(
        user_id=uuid4(),
        version=3,
        updated_at=datetime.now(UTC),
        schema_version=2,
        candidate_topics=["抛物线（1 批）"],
    )
    text = render_index(doc)

    assert "## 候选主题（悬空链接）" in text
    assert "- 抛物线（1 批）" in text
    parsed = parse_index(text)
    assert parsed.candidate_topics == ["抛物线（1 批）"]
    assert parsed.schema_version == 2


def test_index_v2_omits_candidate_section_when_empty() -> None:
    """没有候选时不渲染这一节：既有 index 形状不变（避免无谓 diff）。"""
    from uuid import uuid4

    from backend.memory.storage.markdown_schema import IndexDocument, render_index

    text = render_index(
        IndexDocument(user_id=uuid4(), version=1, updated_at=datetime.now(UTC), schema_version=2)
    )

    assert "候选主题" not in text
