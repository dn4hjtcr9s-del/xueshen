"""Markdown Schema：三类文档的 front matter、确定性渲染与 round-trip 解析。

对应规格 §8.1 / §8.2。渲染必须可解析 round-trip；解析失败的活动版本
触发 checksum/一致性维护告警。
front matter 字符串值一律 JSON 引号序列化，保证确定性往返。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

FRONT_MATTER_BOUNDARY = "---"
MAX_MATERIALIZED_EVIDENCE_REFS = 100


class MarkdownParseError(ValueError):
    """活动版本解析失败：调用方应触发一致性维护告警。"""


# ---------------------------------------------------------------------------
# front matter
# ---------------------------------------------------------------------------


def _serialize_scalar(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return json.dumps(str(value), ensure_ascii=False)


def _parse_scalar(text: str) -> object:
    text = text.strip()
    if text == "null":
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    if text.startswith('"'):
        return json.loads(text)
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def render_front_matter(fields: dict[str, object]) -> str:
    lines = [FRONT_MATTER_BOUNDARY]
    for key, value in fields.items():
        lines.append(f"{key}: {_serialize_scalar(value)}")
    lines.append(FRONT_MATTER_BOUNDARY)
    return "\n".join(lines)


def parse_front_matter(text: str) -> tuple[dict[str, object], str]:
    """返回 (fields, body)。缺少边界或字段非法时抛出 MarkdownParseError。"""
    lines = text.split("\n")
    if not lines or lines[0].strip() != FRONT_MATTER_BOUNDARY:
        raise MarkdownParseError("缺少 front matter 起始边界")
    fields: dict[str, object] = {}
    end_index = -1
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == FRONT_MATTER_BOUNDARY:
            end_index = i
            break
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise MarkdownParseError(f"front matter 行缺少冒号: {line!r}")
        fields[key.strip()] = _parse_scalar(value)
    if end_index < 0:
        raise MarkdownParseError("缺少 front matter 结束边界")
    body = "\n".join(lines[end_index + 1 :]).lstrip("\n")
    return fields, body


def _require_str(fields: dict[str, object], key: str) -> str:
    value = fields.get(key)
    if not isinstance(value, str) or not value:
        raise MarkdownParseError(f"front matter 字段 {key} 缺失或非法")
    return value


def _require_int(fields: dict[str, object], key: str) -> int:
    value = fields.get(key)
    if not isinstance(value, int):
        raise MarkdownParseError(f"front matter 字段 {key} 缺失或非法")
    return value


def _optional_float(fields: dict[str, object], key: str) -> float | None:
    value = fields.get(key)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raise MarkdownParseError(f"front matter 字段 {key} 非法")


def _parse_rfc3339(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _format_rfc3339(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# 正文 sections
# ---------------------------------------------------------------------------

_SECTION_PATTERN = re.compile(r"^## (.+)$", re.MULTILINE)


def _render_sections(title: str, sections: list[tuple[str, list[str] | str]]) -> str:
    """空章节保留标题，不写入空列表项（§8.2）。"""
    parts = [f"# {title}", ""]
    for heading, content in sections:
        parts.append(f"## {heading}")
        parts.append("")
        if isinstance(content, str):
            if content.strip():
                parts.append(content.strip())
                parts.append("")
        else:
            if content:
                parts.extend(f"- {item}" for item in content)
                parts.append("")
    return "\n".join(parts).rstrip("\n") + "\n"


def _parse_sections(body: str) -> tuple[str, dict[str, str]]:
    """返回 (主标题, {章节名: 章节原文})。丢失主标题或章节重复时报错。"""
    lines = body.split("\n")
    if not lines or not lines[0].startswith("# "):
        raise MarkdownParseError("缺少主标题")
    title = lines[0][2:].strip()
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in lines[1:]:
        m = _SECTION_PATTERN.match(line)
        if m:
            if current is not None:
                if current in sections:
                    raise MarkdownParseError(f"章节重复: {current}")
                sections[current] = "\n".join(buffer).strip()
            current = m.group(1).strip()
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        if current in sections:
            raise MarkdownParseError(f"章节重复: {current}")
        sections[current] = "\n".join(buffer).strip()
    return title, sections


def _parse_list_items(section_text: str) -> list[str]:
    items: list[str] = []
    for line in section_text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if not line.startswith("- "):
            raise MarkdownParseError(f"列表章节含非列表行: {line!r}")
        items.append(line[2:].strip())
    return items


# ---------------------------------------------------------------------------
# 文档模型
# ---------------------------------------------------------------------------


@dataclass
class LearnerDocument:
    user_id: UUID
    version: int
    updated_at: datetime
    preferences: list[str] = field(default_factory=list)
    goals: list[str] = field(default_factory=list)
    plans: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float | None = None


@dataclass
class MasteryDocument:
    user_id: UUID
    topic_key: str
    topic_title: str
    version: int
    updated_at: datetime
    overview: str = ""
    understood: list[str] = field(default_factory=list)
    difficulties: list[str] = field(default_factory=list)
    review_advice: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)
    confidence: float | None = None


@dataclass
class IndexEntry:
    memory_id: str
    memory_type: str
    topic_key: str | None
    title: str
    version: int
    updated_at: datetime


@dataclass
class IndexDocument:
    user_id: UUID
    version: int
    updated_at: datetime
    learner: IndexEntry | None = None
    mastery_entries: list[IndexEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def render_learner(doc: LearnerDocument) -> str:
    fm = render_front_matter(
        {
            "kind": "learner-profile",
            "schema_version": 1,
            "user_id": str(doc.user_id),
            "memory_id": "learner",
            "version": doc.version,
            "updated_at": _format_rfc3339(doc.updated_at),
            "evidence_count": len(doc.evidence_refs),
            "confidence": doc.confidence,
        }
    )
    body = _render_sections(
        "学习者档案",
        [
            ("学习偏好", doc.preferences),
            ("学习目标", doc.goals),
            ("当前计划", doc.plans),
            ("证据引用", doc.evidence_refs[:MAX_MATERIALIZED_EVIDENCE_REFS]),
        ],
    )
    return f"{fm}\n\n{body}"


def render_mastery(doc: MasteryDocument) -> str:
    fm = render_front_matter(
        {
            "kind": "mastery-profile",
            "schema_version": 1,
            "user_id": str(doc.user_id),
            "memory_id": f"mastery:{doc.topic_key}",
            "topic_key": doc.topic_key,
            "topic_title": doc.topic_title,
            "version": doc.version,
            "updated_at": _format_rfc3339(doc.updated_at),
            "evidence_count": len(doc.evidence_refs),
            "confidence": doc.confidence,
        }
    )
    body = _render_sections(
        doc.topic_title,
        [
            ("当前掌握概况", doc.overview),
            ("已掌握", doc.understood),
            ("仍有困难", doc.difficulties),
            ("建议复习", doc.review_advice),
            ("证据引用", doc.evidence_refs[:MAX_MATERIALIZED_EVIDENCE_REFS]),
        ],
    )
    return f"{fm}\n\n{body}"


_INDEX_ITEM_PATTERN = re.compile(
    r"^(?P<memory_id>\S+) \| (?P<title>.*?) \| v(?P<version>\d+) \| (?P<updated_at>\S+)$"
)


def _render_index_item(entry: IndexEntry) -> str:
    return (
        f"{entry.memory_id} | {entry.title} | v{entry.version} | "
        f"{_format_rfc3339(entry.updated_at)}"
    )


def render_index(doc: IndexDocument) -> str:
    fm = render_front_matter(
        {
            "kind": "memory-index",
            "schema_version": 1,
            "user_id": str(doc.user_id),
            "memory_id": "index",
            "version": doc.version,
            "updated_at": _format_rfc3339(doc.updated_at),
        }
    )
    learner_items = [_render_index_item(doc.learner)] if doc.learner else []
    mastery_items = [_render_index_item(e) for e in doc.mastery_entries]
    routes = sorted({e.topic_key for e in doc.mastery_entries if e.topic_key})
    body = _render_sections(
        "长期记忆目录",
        [
            ("学习者档案", learner_items),
            ("掌握档案", mastery_items),
            ("主题路由", routes),
        ],
    )
    return f"{fm}\n\n{body}"


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


def parse_learner(text: str) -> LearnerDocument:
    fields, body = parse_front_matter(text)
    if _require_str(fields, "kind") != "learner-profile":
        raise MarkdownParseError("kind 不是 learner-profile")
    title, sections = _parse_sections(body)
    if title != "学习者档案":
        raise MarkdownParseError("主标题不是 学习者档案")
    return LearnerDocument(
        user_id=UUID(_require_str(fields, "user_id")),
        version=_require_int(fields, "version"),
        updated_at=_parse_rfc3339(_require_str(fields, "updated_at")),
        preferences=_parse_list_items(sections.get("学习偏好", "")),
        goals=_parse_list_items(sections.get("学习目标", "")),
        plans=_parse_list_items(sections.get("当前计划", "")),
        evidence_refs=_parse_list_items(sections.get("证据引用", "")),
        confidence=_optional_float(fields, "confidence"),
    )


def parse_mastery(text: str) -> MasteryDocument:
    fields, body = parse_front_matter(text)
    if _require_str(fields, "kind") != "mastery-profile":
        raise MarkdownParseError("kind 不是 mastery-profile")
    topic_key = _require_str(fields, "topic_key")
    topic_title = _require_str(fields, "topic_title")
    title, sections = _parse_sections(body)
    if title != topic_title:
        raise MarkdownParseError("主标题与 topic_title 不一致")
    return MasteryDocument(
        user_id=UUID(_require_str(fields, "user_id")),
        topic_key=topic_key,
        topic_title=topic_title,
        version=_require_int(fields, "version"),
        updated_at=_parse_rfc3339(_require_str(fields, "updated_at")),
        overview=sections.get("当前掌握概况", ""),
        understood=_parse_list_items(sections.get("已掌握", "")),
        difficulties=_parse_list_items(sections.get("仍有困难", "")),
        review_advice=_parse_list_items(sections.get("建议复习", "")),
        evidence_refs=_parse_list_items(sections.get("证据引用", "")),
        confidence=_optional_float(fields, "confidence"),
    )


def _parse_index_item(line: str) -> IndexEntry:
    m = _INDEX_ITEM_PATTERN.match(line)
    if not m:
        raise MarkdownParseError(f"index 条目格式非法: {line!r}")
    memory_id = m.group("memory_id")
    if memory_id == "learner":
        memory_type, topic_key = "learner", None
    elif memory_id.startswith("mastery:"):
        memory_type, topic_key = "mastery", memory_id.removeprefix("mastery:")
    else:
        raise MarkdownParseError(f"index 条目 memory_id 非法: {memory_id!r}")
    return IndexEntry(
        memory_id=memory_id,
        memory_type=memory_type,
        topic_key=topic_key,
        title=m.group("title"),
        version=int(m.group("version")),
        updated_at=_parse_rfc3339(m.group("updated_at")),
    )


def parse_index(text: str) -> IndexDocument:
    fields, body = parse_front_matter(text)
    if _require_str(fields, "kind") != "memory-index":
        raise MarkdownParseError("kind 不是 memory-index")
    _title, sections = _parse_sections(body)
    learner_items = _parse_list_items(sections.get("学习者档案", ""))
    mastery_items = _parse_list_items(sections.get("掌握档案", ""))
    return IndexDocument(
        user_id=UUID(_require_str(fields, "user_id")),
        version=_require_int(fields, "version"),
        updated_at=_parse_rfc3339(_require_str(fields, "updated_at")),
        learner=_parse_index_item(learner_items[0]) if learner_items else None,
        mastery_entries=[_parse_index_item(item) for item in mastery_items],
    )
