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
from typing import TypedDict
from uuid import UUID

FRONT_MATTER_BOUNDARY = "---"

#: schema v1：单行 index 条目，frontmatter 无 name/description/aliases。
SCHEMA_VERSION_V1 = 1
#: schema v2：frontmatter 增加 name/description/aliases，index 改为块结构，
#: 正文支持 `[[link]]` 互链（memory-rebuild §3.2 / §3.4）。
SCHEMA_VERSION_V2 = 2
#: 新建文档使用的版本。历史 versions/ 永不原地迁移（§2.7 决议 A 组）。
CURRENT_SCHEMA_VERSION = SCHEMA_VERSION_V2

#: `[[name]]` / `[[memory_id]]` 互链（§3.2）。悬空链接合法，不做存在性校验。
_LINK_PATTERN = re.compile(r"\[\[([^\[\]]+?)\]\]")
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
    if isinstance(value, (list, tuple)):
        # §3.2 的 frontmatter 示例就是 ``aliases: ["ellipse", "椭圆形"]``；
        # JSON 数组同时也是合法的 YAML 流式序列，且可确定性往返。
        return json.dumps([str(item) for item in value], ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _parse_scalar(text: str) -> object:
    text = text.strip()
    if text == "null":
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    if text.startswith('"') or text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise MarkdownParseError(f"front matter 取值非法: {text!r}") from exc
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        pass
    return text


def extract_links(text: str) -> list[str]:
    """提取正文里的 `[[link]]` 目标，去重且保持出现顺序。

    悬空链接（指向尚未建档的主题）**合法**：§3.2 明确要求照常链接，由每日批量
    总结收集为候选新主题，因此这里不做任何存在性校验。
    """
    seen: set[str] = set()
    found: list[str] = []
    for raw in _LINK_PATTERN.findall(text or ""):
        target = raw.strip()
        if target and target not in seen:
            seen.add(target)
            found.append(target)
    return found


def normalize_aliases(values: list[str] | None) -> list[str]:
    """aliases 规范化：去首尾空白、丢空串、去重（保序）。

    不改大小写也不做 Unicode 折叠：别名是**检索键**，折叠会让"椭圆"与"椭圆形"
    这类本应区分的写法相互吞并。
    """
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        item = str(value).strip()
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def document_links(*bodies: list[str] | str) -> list[str]:
    """汇总若干章节正文里的 `[[link]]`。"""
    collected: list[str] = []
    for body in bodies:
        collected.extend(extract_links(body if isinstance(body, str) else "\n".join(body)))
    return normalize_aliases(collected)


def _v2_front_matter(doc: object) -> dict[str, object]:
    """v2 文档额外写入 name/description/aliases；v1 文档返回空 dict。

    刻意按 ``doc.schema_version`` 分派而不是"有值就写"：v1 历史文件读进来再写回去
    必须仍是 v1（§5.6 明令不得把 v1 序列化成 v2 覆盖原文件）。
    """
    if getattr(doc, "schema_version", SCHEMA_VERSION_V1) < SCHEMA_VERSION_V2:
        return {}
    return {
        "name": getattr(doc, "name", None) or "",
        "description": getattr(doc, "description", None) or "",
        "aliases": list(getattr(doc, "aliases", []) or []),
    }


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


def _schema_version_of(fields: dict[str, object]) -> int:
    """读 front matter 的 schema_version；缺失按 v1 处理（历史文件兼容）。"""
    raw = fields.get("schema_version", SCHEMA_VERSION_V1)
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 1:
        raise MarkdownParseError(f"front matter 字段 schema_version 非法: {raw!r}")
    return raw


def _optional_str_list(fields: dict[str, object], key: str) -> list[str]:
    raw = fields.get(key)
    if raw is None or raw == "":
        return []
    if isinstance(raw, str):
        # 兼容手写的 `a | b` 形式，避免一个分隔符写法差异就让整篇文档解析失败
        return [part.strip() for part in raw.split("|") if part.strip()]
    if isinstance(raw, list):
        return [str(item) for item in raw]
    raise MarkdownParseError(f"front matter 字段 {key} 必须是字符串列表")


class V2ParsedFields(TypedDict, total=False):
    """v2 解析出的可选字段集合。

    用 TypedDict 而不是 ``dict[str, object]``：后者在 ``**`` 展开进 dataclass 时
    会让 mypy 失去字段级类型校验（strict 下直接报 arg-type）。
    """

    name: str
    description: str
    aliases: list[str]
    links: list[str]


def _v2_parsed_fields(fields: dict[str, object], links: list[str]) -> V2ParsedFields:
    """v2 文档的 name/description/aliases/links；v1 文档返回空 dict。

    v2 下 name 与 description 都是**必填**（§3.6：create 必须生成它们，否则注册表
    目录无法回答"这个文件里有什么"）；description 还必须是单行。
    """
    if _schema_version_of(fields) < SCHEMA_VERSION_V2:
        return V2ParsedFields()
    name = _require_str(fields, "name")
    description = _require_str(fields, "description")
    if "\n" in description or "\r" in description:
        raise MarkdownParseError("front matter 字段 description 必须是单行")
    return V2ParsedFields(
        name=name,
        description=description,
        aliases=normalize_aliases(_optional_str_list(fields, "aliases")),
        links=links,
    )


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
    #: 原始 schema 版本。**必须保留**：v1 历史文件读进来还是 v1，渲染回去不得变成 v2
    #: （§5.6「不能把 v1 历史文件序列化成 v2 后覆盖原文件」）。
    schema_version: int = SCHEMA_VERSION_V1
    #: v2 新增：链接显示名 / 一行描述 / 实体别名 / 正文 `[[link]]` 目标。
    name: str | None = None
    description: str | None = None
    aliases: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
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
    schema_version: int = SCHEMA_VERSION_V1
    name: str | None = None
    description: str | None = None
    aliases: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
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
    #: v2 决策：``title`` 就是 index 里的 ``name``、``summary`` 就是 ``description``
    #: （同一份投影不再开两列），因此这里不再单独存 name/description。
    title: str
    version: int
    updated_at: datetime
    #: v2：index 条目的一行描述。落 PG 时复用既有 ``summary`` 列（Phase 4 决策）。
    description: str = ""
    #: v2 新增投影：实体别名与 `[[link]]` 指向的相邻主题。
    aliases: list[str] = field(default_factory=list)
    related_topic_keys: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)


@dataclass
class IndexDocument:
    user_id: UUID
    version: int
    updated_at: datetime
    schema_version: int = SCHEMA_VERSION_V1
    learner: IndexEntry | None = None
    mastery_entries: list[IndexEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------


def render_learner(doc: LearnerDocument) -> str:
    fm = render_front_matter(
        {
            "kind": "learner-profile",
            "schema_version": doc.schema_version,
            "user_id": str(doc.user_id),
            "memory_id": "learner",
            "version": doc.version,
            "updated_at": _format_rfc3339(doc.updated_at),
            "evidence_count": len(doc.evidence_refs),
            "confidence": doc.confidence,
            **_v2_front_matter(doc),
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
            "schema_version": doc.schema_version,
            "user_id": str(doc.user_id),
            "memory_id": f"mastery:{doc.topic_key}",
            "topic_key": doc.topic_key,
            "topic_title": doc.topic_title,
            "version": doc.version,
            "updated_at": _format_rfc3339(doc.updated_at),
            "evidence_count": len(doc.evidence_refs),
            "confidence": doc.confidence,
            **_v2_front_matter(doc),
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


#: v2 index 的块内字段顺序（渲染与解析共用，保证确定性往返）。
_V2_INDEX_FIELDS = (
    "name",
    "description",
    "aliases",
    "keywords",
    "related",
    "version",
    "updated_at",
)


def _render_index_block(entry: IndexEntry) -> list[str]:
    """v2 index 条目块：``### <memory_id>`` + 若干 ``- key: value`` 行。

    块结构取代 v1 的单行 ``a | b | v1 | ts``：keywords/aliases/related 都是列表，
    塞进单行会让分隔符与内容互相转义，块结构可读性也更好（§2.7 决议 A 组接受升版）。
    """
    lines = [f"### {entry.memory_id}"]
    values: dict[str, object] = {
        "name": entry.title,
        "description": entry.description,
        "aliases": entry.aliases,
        "keywords": entry.keywords,
        "related": entry.related_topic_keys,
        "version": entry.version,
        "updated_at": _format_rfc3339(entry.updated_at),
    }
    list_values: dict[str, list[str]] = {
        "aliases": entry.aliases,
        "keywords": entry.keywords,
        "related": entry.related_topic_keys,
    }
    for key in _V2_INDEX_FIELDS:
        if key in list_values:
            rendered = " | ".join(list_values[key])
        elif key == "version":
            rendered = f"v{entry.version}"
        else:
            rendered = str(values[key])
        lines.append(f"- {key}: {rendered}")
    return lines


def render_index(doc: IndexDocument) -> str:
    if doc.schema_version >= SCHEMA_VERSION_V2:
        return _render_index_v2(doc)
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


def _render_index_v2(doc: IndexDocument) -> str:
    """v2 index：frontmatter 声明 schema_version 2，条目改为块结构。"""
    fm = render_front_matter(
        {
            "kind": "memory-index",
            "schema_version": SCHEMA_VERSION_V2,
            "user_id": str(doc.user_id),
            "memory_id": "index",
            "version": doc.version,
            "updated_at": _format_rfc3339(doc.updated_at),
        }
    )
    parts = ["# 长期记忆目录", "", "## 学习者档案", ""]
    if doc.learner is not None:
        parts.extend(_render_index_block(doc.learner))
        parts.append("")
    parts.extend(["## 掌握档案", ""])
    for entry in doc.mastery_entries:
        parts.extend(_render_index_block(entry))
        parts.append("")
    parts.extend(["## 主题路由", ""])
    routes = sorted({e.topic_key for e in doc.mastery_entries if e.topic_key})
    parts.extend(f"- {route}" for route in routes)
    return f"{fm}\n\n" + "\n".join(parts).rstrip("\n") + "\n"


def parse_learner(text: str) -> LearnerDocument:
    fields, body = parse_front_matter(text)
    if _require_str(fields, "kind") != "learner-profile":
        raise MarkdownParseError("kind 不是 learner-profile")
    title, sections = _parse_sections(body)
    if title != "学习者档案":
        raise MarkdownParseError("主标题不是 学习者档案")
    preferences = _parse_list_items(sections.get("学习偏好", ""))
    goals = _parse_list_items(sections.get("学习目标", ""))
    plans = _parse_list_items(sections.get("当前计划", ""))
    return LearnerDocument(
        user_id=UUID(_require_str(fields, "user_id")),
        version=_require_int(fields, "version"),
        updated_at=_parse_rfc3339(_require_str(fields, "updated_at")),
        schema_version=_schema_version_of(fields),
        preferences=preferences,
        goals=goals,
        plans=plans,
        evidence_refs=_parse_list_items(sections.get("证据引用", "")),
        confidence=_optional_float(fields, "confidence"),
        **_v2_parsed_fields(fields, document_links(preferences, goals, plans)),
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
    overview = sections.get("当前掌握概况", "")
    understood = _parse_list_items(sections.get("已掌握", ""))
    difficulties = _parse_list_items(sections.get("仍有困难", ""))
    review_advice = _parse_list_items(sections.get("建议复习", ""))
    return MasteryDocument(
        user_id=UUID(_require_str(fields, "user_id")),
        topic_key=topic_key,
        topic_title=topic_title,
        version=_require_int(fields, "version"),
        updated_at=_parse_rfc3339(_require_str(fields, "updated_at")),
        schema_version=_schema_version_of(fields),
        overview=overview,
        understood=understood,
        difficulties=difficulties,
        review_advice=review_advice,
        evidence_refs=_parse_list_items(sections.get("证据引用", "")),
        confidence=_optional_float(fields, "confidence"),
        **_v2_parsed_fields(
            fields, document_links(overview, understood, difficulties, review_advice)
        ),
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


def _parse_index_blocks(section_text: str, *, memory_type: str) -> list[IndexEntry]:
    """解析 v2 块结构：``### <memory_id>`` 起始，后跟 ``- key: value`` 行。"""
    entries: list[IndexEntry] = []
    current_id: str | None = None
    values: dict[str, str] = {}

    def _flush() -> None:
        if current_id:
            entries.append(_index_entry_from_block(current_id, values, memory_type))

    for raw_line in section_text.split("\n"):
        line = raw_line.rstrip()
        if line.startswith("### "):
            _flush()
            current_id = line[4:].strip()
            values = {}
            continue
        if not line.startswith("- "):
            continue
        key, sep, value = line[2:].partition(":")
        if not sep:
            continue
        values[key.strip()] = value.strip()
    # 循环内不能用哨兵行收尾：哨兵会被 rstrip 掉尾空格而不再匹配 "### "
    _flush()
    return entries


def _split_pipe(value: str) -> list[str]:
    return [part.strip() for part in value.split("|") if part.strip()]


def _index_entry_from_block(memory_id: str, values: dict[str, str], memory_type: str) -> IndexEntry:
    version_raw = values.get("version", "v0").lstrip("v")
    topic_key = memory_id.removeprefix("mastery:") if memory_type == "mastery" else None
    return IndexEntry(
        memory_id=memory_id,
        memory_type=memory_type,
        topic_key=topic_key,
        title=values.get("name", ""),
        version=int(version_raw) if version_raw.isdigit() else 0,
        updated_at=(
            _parse_rfc3339(values["updated_at"]) if values.get("updated_at") else datetime.now(UTC)
        ),
        description=values.get("description", ""),
        aliases=_split_pipe(values.get("aliases", "")),
        related_topic_keys=_split_pipe(values.get("related", "")),
        keywords=_split_pipe(values.get("keywords", "")),
    )


def parse_index(text: str) -> IndexDocument:
    fields, body = parse_front_matter(text)
    if _require_str(fields, "kind") != "memory-index":
        raise MarkdownParseError("kind 不是 memory-index")
    _title, sections = _parse_sections(body)
    schema_version = _schema_version_of(fields)
    if schema_version >= SCHEMA_VERSION_V2:
        learner_blocks = _parse_index_blocks(sections.get("学习者档案", ""), memory_type="learner")
        mastery_blocks = _parse_index_blocks(sections.get("掌握档案", ""), memory_type="mastery")
        return IndexDocument(
            user_id=UUID(_require_str(fields, "user_id")),
            version=_require_int(fields, "version"),
            updated_at=_parse_rfc3339(_require_str(fields, "updated_at")),
            schema_version=schema_version,
            learner=learner_blocks[0] if learner_blocks else None,
            mastery_entries=mastery_blocks,
        )
    learner_items = _parse_list_items(sections.get("学习者档案", ""))
    mastery_items = _parse_list_items(sections.get("掌握档案", ""))
    return IndexDocument(
        user_id=UUID(_require_str(fields, "user_id")),
        version=_require_int(fields, "version"),
        updated_at=_parse_rfc3339(_require_str(fields, "updated_at")),
        schema_version=schema_version,
        learner=_parse_index_item(learner_items[0]) if learner_items else None,
        mastery_entries=[_parse_index_item(item) for item in mastery_items],
    )
