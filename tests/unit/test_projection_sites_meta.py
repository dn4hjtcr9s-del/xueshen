"""多处投影站点的**全量枚举型元测试**（用户元问题：「修一处、漏同类」）。

## 为什么需要它

同一件事——"文档 → 注册表 / index.md / PG 投影"——在这份代码里有**多个站点**。
review I-11③ 只改了提交路径，``restore`` / ``rebuild_index`` / ``refresh_index_projection``
仍在用 ``topic_title``；新发现 7 补上其中两处后，``consolidation._mastery_view`` 还是
第五处。逐点补回归测试永远追不上"下一个同类站点"，所以这里改成枚举型元测试：

1. **自动枚举站点**：AST 扫 ``backend/memory/**``，凡"引用了投影管线的权威符号"或
   "写了含 ≥2 个注册表字段 / ≥3 个投影字段的字典字面量"的函数，都必须是已登记站点
   （``SITES``）。**新加一个投影站点而不登记，本文件立刻失败**；
2. **字段清单**：``REGISTRY_PAYLOAD_KEYS`` / ``FRONTMATTER_KEYS`` / ``INDEX_BLOCK_KEYS``
   / ``PG_PROJECTION_COLUMNS`` 是显式清单，站点实际写/读的键集合必须与之**恰好相等**
   （多一个键、少一个键都失败）——**给投影新增一个字段，必须同时改清单与所有站点**；
3. **取值来源真值表**：给每个可调用站点喂一份"每个权威来源都取唯一哨兵值"的合成
   文档，断言它输出的每个键都等于**登记的那个来源**。站点把 ``name`` 换回
   ``topic_title``、或漏掉 ``aliases``，都会在这一层被抓到；
4. **跨站点一致**：提交 / 恢复 / 迁移刷新三条写入路径捕获到的投影载荷必须
   **逐键相等**（喂同一个文档）；``rebuild_index`` 重建出的 index.md 必须与注册表投影
   一致。

## 已知的、刻意不一致的一处（写进真值表，不是漏掉）

注册表 ``summary``（= index.md 的 ``- description``）取**正文概述**（mastery ``overview``
/ learner ``goals`` 拼接），**不是** frontmatter ``description``。这是 DEV-023 明确
"接受现状并登记"的取舍（``memory_index_entries.summary`` 同时是 ``memory.search`` 的
匹配域，改动它属独立的检索语义变更）。真值表把它写成两类来源（``BODY_SUMMARY`` vs
``DESCRIPTION``）；谁要改这个决定，必须同时改 ``SITES`` 里的来源标注——这正是元测试
要的效果。

## 覆盖清单（``SITES``）

- frontmatter 写入：``_v2_front_matter``、``render_learner``、``render_mastery``、
  ``maintenance._upgrade_to_schema_v2``（迁移用它们重渲染 v2 文档）；
- index 渲染/解析：``_render_index_block``、``_render_index_v2``、``render_index``、
  ``_parse_index_blocks``、``_index_entry_from_block``、``parse_index``；
- 注册表投影写入：``index_projection_from_document``（唯一权威实现）、
  ``_build_new_content``、``restore``、``refresh_index_projection``、``commit_plans``、
  ``_upsert_index_entry``；
- index.md 重建：``rebuild_index``、``_index_projection``、``_index_entry_from_projection``；
- consolidation 视图：``_mastery_view``、``_learner_view``、``_govern_dangling_links``、
  ``_load_user_documents``。
"""

from __future__ import annotations

import ast
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from backend.memory.contracts.commands import CommitMutationPlan, LearnerPatch, MasteryPatch
from backend.memory.services import memory_service as ms_module
from backend.memory.services.memory_service import (
    LEARNER_PROFILE_TITLE,
    MemoryService,
    index_projection_from_document,
    projected_title,
)
from backend.memory.storage import markdown_schema as schema
from backend.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORY_ROOT = REPO_ROOT / "backend" / "memory"
USER_ID = UUID("00000000-0000-4000-8000-00000000abcd")
NOW = datetime(2026, 9, 12, 3, 0, tzinfo=UTC)

# ---------------------------------------------------------------------------
# 1. 权威字段清单（清单与实现必须同时改）
# ---------------------------------------------------------------------------

#: 注册表投影载荷（``index_data`` / ``memory_index_entries`` 行）的**完整**键集合。
REGISTRY_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {"title", "summary", "keywords", "aliases", "related_topic_keys", "search_text"}
)

#: v2 文档 frontmatter 的投影字段（memory-rebuild §3.2）。
FRONTMATTER_KEYS: frozenset[str] = frozenset({"name", "description", "aliases", "keywords"})

#: v2 index 条目的字段（§3.4：``name | description | aliases | keywords | related |
#: version | updated_at``）。
INDEX_BLOCK_KEYS: frozenset[str] = frozenset(
    {"name", "description", "aliases", "keywords", "related", "version", "updated_at"}
)

#: 写进 ``memory_index_entries`` 的投影列（写入侧 INSERT/UPDATE 全覆盖）。
PG_PROJECTION_COLUMNS: frozenset[str] = REGISTRY_PAYLOAD_KEYS

#: 读回给 index.md 的投影列（``search_text`` 只服务检索，不进 index.md）。
PG_INDEX_READ_COLUMNS: frozenset[str] = REGISTRY_PAYLOAD_KEYS - {"search_text"}

#: 投影字段的所有拼写变体：AST 发现规则用它判断一个 dict 是不是投影载荷。
PROJECTION_SPELLINGS: frozenset[str] = FRONTMATTER_KEYS | INDEX_BLOCK_KEYS | REGISTRY_PAYLOAD_KEYS

#: 注册表载荷的"核心键"（只出现在真正的注册表投影里，用于发现规则）。
REGISTRY_CORE_KEYS: frozenset[str] = frozenset(
    {"title", "summary", "aliases", "keywords", "related_topic_keys"}
)

#: 投影字段的规范名 → 中文说明（报告与失败信息都用它）。
PROJECTION_FIELDS: dict[str, str] = {
    "name": "注册表/index 的标题：v2 取 frontmatter name，v1 取 topic_title / 固定标签",
    "description": "index.md 的单行描述：取注册表 summary（DEV-023），frontmatter 另有同名键",
    "aliases": "实体别名（检索键，文档是唯一事实源）",
    "keywords": "判别性检索词（生产者是 consolidation）",
    "related_topic_keys": "正文 `[[link]]` 指向的相邻主题",
}


class Source(StrEnum):
    """投影取值的**权威来源**；合成探针给每个来源一个唯一取值。"""

    #: ``projected_title(doc)``
    TITLE = "title"
    #: 文档 frontmatter 的 ``name``（只写文档、不写注册表的站点）
    NAME = "name"
    #: 文档 frontmatter 的 ``description``
    DESCRIPTION = "description"
    #: 正文概述：mastery ``overview`` / learner ``goals`` 拼接（DEV-023）
    BODY_SUMMARY = "body_summary"
    ALIASES = "aliases"
    KEYWORDS = "keywords"
    #: 从**渲染后的正文**现算的 ``[[link]]``
    RENDERED_LINKS = "rendered_links"
    #: 检索域（标题 + aliases + 正文各段拼接）
    SEARCH_TEXT = "search_text"
    #: 透传：值由调用方喂进来，站点必须原样送出（渲染/落库/读回站点）
    PASSTHROUGH = "passthrough"


# ---------------------------------------------------------------------------
# 2. 合成探针：每个权威来源一个唯一取值
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocProbe:
    """一份合成文档 + 它的哨兵取值（同名来源处处不同，用错来源立刻暴露）。"""

    doc: Any
    memory_type: str
    title: str
    name: str | None
    topic_title: str | None
    description: str
    body_summary: str
    aliases: list[str]
    keywords: list[str]
    links: list[str]
    body_terms: list[str]
    search_text: str

    def source_value(self, source: Source, field_name: str) -> Any:
        """按真值表取该来源的值（``field_name`` 只用于失败信息）。"""
        match source:
            case Source.TITLE:
                return self.title
            case Source.NAME:
                return self.name
            case Source.DESCRIPTION:
                return self.description
            case Source.BODY_SUMMARY:
                return self.body_summary
            case Source.ALIASES:
                return self.aliases
            case Source.KEYWORDS:
                return self.keywords
            case Source.RENDERED_LINKS:
                return self.links
            case Source.SEARCH_TEXT:
                return self.search_text
            case Source.PASSTHROUGH:
                raise AssertionError(f"PASSTHROUGH 不比对取值: {field_name}")
        raise AssertionError(f"未覆盖的来源: {source}")


def _search_text(title: str, aliases: list[str], body_terms: list[str]) -> str:
    return " ".join([title, *aliases, *body_terms])


def _mastery_probe() -> DocProbe:
    name = "PROBE-NAME-7f3a"
    topic_title = "PROBE-TOPIC-TITLE-b21c"
    description = "PROBE-DESCRIPTION-55d0"
    overview = "PROBE-OVERVIEW-9e11 与 [[PROBE-LINK-1]]、[[PROBE-LINK-2]] 相关"
    aliases = ["PROBE-ALIAS-1", "PROBE-ALIAS-2"]
    keywords = ["PROBE-KEYWORD-1", "PROBE-KEYWORD-2"]
    links = ["PROBE-LINK-1", "PROBE-LINK-2"]
    understood = ["PROBE-UNDERSTOOD-1"]
    difficulties = ["PROBE-DIFFICULTY-1"]
    review_advice = ["PROBE-REVIEW-1"]
    body_terms = [overview, *understood, *difficulties, *review_advice]
    doc = schema.MasteryDocument(
        user_id=USER_ID,
        topic_key="probe-topic",
        topic_title=topic_title,
        version=2,
        updated_at=NOW,
        schema_version=schema.SCHEMA_VERSION_V2,
        name=name,
        description=description,
        aliases=list(aliases),
        keywords=list(keywords),
        links=list(links),
        overview=overview,
        understood=list(understood),
        difficulties=list(difficulties),
        review_advice=list(review_advice),
        evidence_refs=["evidence-1"],
        confidence=0.5,
    )
    return DocProbe(
        doc=doc,
        memory_type="mastery",
        title=name,
        name=name,
        topic_title=topic_title,
        description=description,
        body_summary=overview,
        aliases=list(aliases),
        keywords=list(keywords),
        links=list(links),
        body_terms=body_terms,
        search_text=_search_text(name, aliases, body_terms),
    )


def _learner_probe() -> DocProbe:
    name = "PROBE-LEARNER-NAME-1c9d"
    description = "PROBE-LEARNER-DESCRIPTION-4a7e"
    aliases = ["PROBE-LEARNER-ALIAS-1"]
    keywords = ["PROBE-LEARNER-KEYWORD-1"]
    links = ["PROBE-LEARNER-LINK-1"]
    preferences = ["PROBE-PREFERENCE-1"]
    goals = ["PROBE-GOAL-1", "PROBE-GOAL-2"]
    plans = ["PROBE-PLAN-1 见 [[PROBE-LEARNER-LINK-1]]"]
    body_terms = [*preferences, *goals, *plans]
    doc = schema.LearnerDocument(
        user_id=USER_ID,
        version=3,
        updated_at=NOW,
        schema_version=schema.SCHEMA_VERSION_V2,
        name=name,
        description=description,
        aliases=list(aliases),
        keywords=list(keywords),
        links=list(links),
        preferences=list(preferences),
        goals=list(goals),
        plans=list(plans),
        evidence_refs=["evidence-2"],
    )
    return DocProbe(
        doc=doc,
        memory_type="learner",
        title=name,
        name=name,
        topic_title=None,
        description=description,
        body_summary="；".join(goals[:3]),
        aliases=list(aliases),
        keywords=list(keywords),
        links=list(links),
        body_terms=body_terms,
        search_text=_search_text(name, aliases, body_terms),
    )


def _mastery_v1_probe() -> DocProbe:
    """v1 文档：解析器保证 name/description/aliases/keywords/links 全为空。"""
    topic_title = "PROBE-V1-TOPIC-TITLE"
    overview = "PROBE-V1-OVERVIEW"
    understood = "PROBE-V1-UNDERSTOOD"
    body_terms = [overview, understood]
    doc = schema.MasteryDocument(
        user_id=USER_ID,
        topic_key="probe-v1",
        topic_title=topic_title,
        version=1,
        updated_at=NOW,
        schema_version=schema.SCHEMA_VERSION_V1,
        overview=overview,
        understood=[understood],
    )
    return DocProbe(
        doc=doc,
        memory_type="mastery",
        title=topic_title,
        name=None,
        topic_title=topic_title,
        description="",
        body_summary=overview,
        aliases=[],
        keywords=[],
        links=[],
        body_terms=body_terms,
        search_text=_search_text(topic_title, [], body_terms),
    )


@dataclass(frozen=True)
class Probe:
    """整套探针（mastery v2 / learner v2 / mastery v1）。"""

    mastery: DocProbe = field(default_factory=_mastery_probe)
    learner: DocProbe = field(default_factory=_learner_probe)
    mastery_v1: DocProbe = field(default_factory=_mastery_v1_probe)


PROBE = Probe()


def _expected_payload(site: ProjectionSite, doc_probe: DocProbe) -> dict[str, Any]:
    """按站点登记的真值表算出期望载荷（透传字段由调用方自己喂，不在此列）。"""
    return {
        output_key: doc_probe.source_value(source, canonical)
        for canonical, (output_key, source) in site.fields.items()
        if source is not Source.PASSTHROUGH
    }


def _assert_payload(site: ProjectionSite, actual: Mapping[str, Any], doc_probe: DocProbe) -> None:
    """断言载荷的键集合与每个键的取值来源都与登记一致。"""
    assert set(actual) == site.expected_keys, (
        f"{site.key} 的输出键集合与登记不符：\n"
        f"  实际 - 登记 = {sorted(set(actual) - site.expected_keys)}\n"
        f"  登记 - 实际 = {sorted(site.expected_keys - set(actual))}\n"
        "新增/删除投影字段时，必须同时更新 SITES 与 *_KEYS 清单。"
    )
    for canonical, (output_key, source) in site.fields.items():
        if source is Source.PASSTHROUGH:
            continue
        expected = doc_probe.source_value(source, canonical)
        assert actual[output_key] == expected, (
            f"{site.key} 的 {output_key!r} 取值来源错了：期望 {source}"
            f"（{expected!r}），实际 {actual[output_key]!r}。"
        )


# ---------------------------------------------------------------------------
# 3. 站点清单
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectionSite:
    """一个投影站点：AST 可定位 + 声明它承载哪些字段、取值来源是什么。"""

    #: 稳定站点 id（失败信息里用它定位）
    key: str
    #: ``backend/memory/.../file.py::qualname``（qualname 支持 ``Class.method``）
    target: str
    #: payload=自己拼投影载荷；delegate=必须调用权威实现；consumer=只透传/分派；
    #: read=读回/透传；construct=构造投影对象
    kind: str
    #: 规范字段 → (该站点输出里的键, 取值来源)
    fields: dict[str, tuple[str, Source]] = field(default_factory=dict)
    #: 刻意**不**承载的规范字段 → 理由（必须写清楚，否则就是漏掉了）
    absent: dict[str, str] = field(default_factory=dict)
    #: 该站点载荷里允许存在的非投影键（例如 learner 的 changed_sections）
    extra_keys: frozenset[str] = frozenset()
    #: delegate/consumer 站点必须出现的管线符号
    must_call: tuple[str, ...] = ()
    #: 该站点期望的输出键集合（None = 由 fields + extra_keys 推导）
    payload_keys: frozenset[str] | None = None

    @property
    def path(self) -> str:
        return self.target.split("::", 1)[0]

    @property
    def qualname(self) -> str:
        return self.target.split("::", 1)[1]

    @property
    def expected_keys(self) -> frozenset[str]:
        if self.payload_keys is not None:
            return self.payload_keys
        return frozenset(key for key, _ in self.fields.values()) | self.extra_keys

    @property
    def expected_spellings(self) -> frozenset[str]:
        return self.expected_keys & PROJECTION_SPELLINGS


def _frontmatter_fields() -> dict[str, tuple[str, Source]]:
    return {
        "name": ("name", Source.NAME),
        "description": ("description", Source.DESCRIPTION),
        "aliases": ("aliases", Source.ALIASES),
        "keywords": ("keywords", Source.KEYWORDS),
    }


def _index_block_fields() -> dict[str, tuple[str, Source]]:
    return {
        "name": ("name", Source.PASSTHROUGH),
        "description": ("description", Source.PASSTHROUGH),
        "aliases": ("aliases", Source.PASSTHROUGH),
        "keywords": ("keywords", Source.PASSTHROUGH),
        "related_topic_keys": ("related", Source.PASSTHROUGH),
    }


def _registry_fields() -> dict[str, tuple[str, Source]]:
    return {
        "name": ("title", Source.TITLE),
        "description": ("summary", Source.BODY_SUMMARY),
        "aliases": ("aliases", Source.ALIASES),
        "keywords": ("keywords", Source.KEYWORDS),
        "related_topic_keys": ("related_topic_keys", Source.RENDERED_LINKS),
        "search_text": ("search_text", Source.SEARCH_TEXT),
    }


def _consumer_site(
    key: str, target: str, *, must_call: tuple[str, ...], note: str
) -> ProjectionSite:
    return ProjectionSite(
        key=key,
        target=target,
        kind="consumer",
        absent={
            name: f"消费/分派站点（{note}）：字段由调用链上的权威实现产出"
            for name in PROJECTION_FIELDS
        },
        must_call=must_call,
        payload_keys=frozenset(),
    )


_LINKS_IN_BODY = "互链只存在于正文 `[[link]]`；frontmatter 不写 links（§3.2）"
_SEARCH_TEXT_ABSENT = "检索域是 PG 列的派生物，frontmatter 不写"

SITES: tuple[ProjectionSite, ...] = (
    # ---------------- 文档 frontmatter 写入侧 ----------------
    ProjectionSite(
        key="schema.v2_front_matter",
        target="backend/memory/storage/markdown_schema.py::_v2_front_matter",
        kind="payload",
        fields=_frontmatter_fields(),
        absent={"related_topic_keys": _LINKS_IN_BODY, "search_text": _SEARCH_TEXT_ABSENT},
    ),
    ProjectionSite(
        key="schema.render_mastery",
        target="backend/memory/storage/markdown_schema.py::render_mastery",
        kind="delegate",
        fields=_frontmatter_fields(),
        absent={"related_topic_keys": _LINKS_IN_BODY, "search_text": _SEARCH_TEXT_ABSENT},
        must_call=("_v2_front_matter",),
    ),
    ProjectionSite(
        key="schema.render_learner",
        target="backend/memory/storage/markdown_schema.py::render_learner",
        kind="delegate",
        fields=_frontmatter_fields(),
        absent={"related_topic_keys": _LINKS_IN_BODY, "search_text": _SEARCH_TEXT_ABSENT},
        must_call=("_v2_front_matter",),
    ),
    ProjectionSite(
        key="maintenance.upgrade_to_schema_v2",
        target="backend/memory/graph/maintenance.py::_upgrade_to_schema_v2",
        kind="delegate",
        fields=_frontmatter_fields(),
        absent={"related_topic_keys": _LINKS_IN_BODY, "search_text": _SEARCH_TEXT_ABSENT},
        must_call=("render_mastery", "render_learner"),
    ),
    # ---------------- index 渲染/解析侧 ----------------
    ProjectionSite(
        key="schema.render_index_block",
        target="backend/memory/storage/markdown_schema.py::_render_index_block",
        kind="payload",
        fields=_index_block_fields(),
        extra_keys=frozenset({"version", "updated_at"}),
    ),
    ProjectionSite(
        key="schema.render_index_v2",
        target="backend/memory/storage/markdown_schema.py::_render_index_v2",
        kind="delegate",
        fields=_index_block_fields(),
        extra_keys=frozenset({"version", "updated_at"}),
        must_call=("_render_index_block",),
    ),
    _consumer_site(
        "schema.render_index",
        "backend/memory/storage/markdown_schema.py::render_index",
        must_call=("_render_index_v2", "_render_index_item"),
        note="按 schema_version 分派到 v1 单行 / v2 块结构",
    ),
    ProjectionSite(
        key="schema.parse_index_blocks",
        target="backend/memory/storage/markdown_schema.py::_parse_index_blocks",
        kind="delegate",
        fields=_index_block_fields(),
        extra_keys=frozenset({"version", "updated_at"}),
        must_call=("_index_entry_from_block",),
    ),
    ProjectionSite(
        key="schema.index_entry_from_block",
        target="backend/memory/storage/markdown_schema.py::_index_entry_from_block",
        # read：既读 `- name:` / `- related:` 这些**渲染侧拼写**，又用 registry 拼写构造
        # IndexEntry（title / related_topic_keys），两侧都要登记，防止渲染与解析漂移。
        kind="read",
        fields=_index_block_fields(),
        extra_keys=frozenset({"version", "updated_at", "title", "related_topic_keys"}),
    ),
    ProjectionSite(
        key="schema.parse_index_item",
        target="backend/memory/storage/markdown_schema.py::_parse_index_item",
        kind="construct",
        fields={"name": ("title", Source.PASSTHROUGH)},
        absent={
            "description": "v1 单行 index 只有 `memory_id | title | v? | ts`，没有 v2 字段",
            "aliases": "同上：v1 单行格式不承载 aliases",
            "keywords": "同上：v1 单行格式不承载 keywords",
            "related_topic_keys": "同上：v1 单行格式不承载 related",
        },
        extra_keys=frozenset({"version", "updated_at"}),
    ),
    _consumer_site(
        "schema.parse_index",
        "backend/memory/storage/markdown_schema.py::parse_index",
        must_call=("_parse_index_blocks",),
        note="v2 走块解析、v1 走单行解析",
    ),
    # ---------------- 注册表投影写入侧 ----------------
    ProjectionSite(
        key="ms.projection_impl",
        target="backend/memory/services/memory_service.py::index_projection_from_document",
        kind="payload",
        fields=_registry_fields(),
        payload_keys=REGISTRY_PAYLOAD_KEYS,
    ),
    ProjectionSite(
        key="ms.build_new_content",
        target="backend/memory/services/memory_service.py::MemoryService._build_new_content",
        kind="delegate",
        fields=_registry_fields(),
        # `changed_sections` 是 learner.updated 事件的载荷，不是投影字段：
        # 取值测试里单独断言，不进这里的 expected_keys（否则 mastery 分支会误报缺失）。
        absent={"changed_sections": "非投影字段：learner.updated 事件用，适配器里单独断言"},
        must_call=("index_projection_from_document",),
    ),
    ProjectionSite(
        key="ms.restore",
        target="backend/memory/services/memory_service.py::MemoryService.restore",
        kind="delegate",
        fields=_registry_fields(),
        must_call=("index_projection_from_document", "_upsert_index_entry"),
    ),
    ProjectionSite(
        key="ms.refresh_index_projection",
        target="backend/memory/services/memory_service.py::MemoryService.refresh_index_projection",
        kind="delegate",
        fields=_registry_fields(),
        must_call=("index_projection_from_document", "_upsert_index_entry"),
    ),
    _consumer_site(
        "ms.commit_plans",
        "backend/memory/services/memory_service.py::MemoryService.commit_plans",
        must_call=("_upsert_index_entry",),
        note="提交编排：载荷由 _build_new_content 产出后原样交给落库站点",
    ),
    ProjectionSite(
        key="ms.upsert_index_entry",
        target="backend/memory/services/memory_service.py::MemoryService._upsert_index_entry",
        kind="payload",
        fields=_registry_fields(),
        extra_keys=frozenset(
            {
                "user_id",
                "memory_id",
                "source_version",
                "memory_type",
                "topic_key",
                "evidence_refs",
                "updated_at",
            }
        ),
        payload_keys=PG_PROJECTION_COLUMNS
        | {
            "user_id",
            "memory_id",
            "source_version",
            "memory_type",
            "topic_key",
            "evidence_refs",
            "updated_at",
        },
    ),
    # ---------------- index.md 重建侧 ----------------
    _consumer_site(
        "ms.rebuild_index",
        "backend/memory/services/memory_service.py::MemoryService.rebuild_index",
        must_call=("_index_projection", "_index_entry_from_projection", "render_index"),
        note="重建不重算投影：读 PG 投影行后交给条目构造与渲染站点",
    ),
    ProjectionSite(
        key="ms.index_projection",
        target="backend/memory/services/memory_service.py::MemoryService._index_projection",
        kind="read",
        # 读回站点：输出键就是 PG 列名（title/summary/…），规范字段映射到这些拼写
        fields={
            "name": ("title", Source.PASSTHROUGH),
            "description": ("summary", Source.PASSTHROUGH),
            "aliases": ("aliases", Source.PASSTHROUGH),
            "keywords": ("keywords", Source.PASSTHROUGH),
            "related_topic_keys": ("related_topic_keys", Source.PASSTHROUGH),
        },
        absent={"search_text": "index.md 不需要检索域（search_text 只服务 memory.search）"},
        extra_keys=frozenset({"updated_at"}),
    ),
    ProjectionSite(
        key="ms.index_entry_from_projection",
        target=(
            "backend/memory/services/memory_service.py::MemoryService._index_entry_from_projection"
        ),
        kind="read",
        fields={
            "name": ("title", Source.PASSTHROUGH),
            "description": ("description", Source.PASSTHROUGH),
            "aliases": ("aliases", Source.PASSTHROUGH),
            "keywords": ("keywords", Source.PASSTHROUGH),
            "related_topic_keys": ("related_topic_keys", Source.PASSTHROUGH),
        },
        absent={"search_text": "index.md 条目不含检索域"},
        # 既读 PG 投影行（.get("summary")），又用 index 拼写构造 IndexEntry
        extra_keys=frozenset({"version", "updated_at", "summary"}),
    ),
    # ---------------- consolidation 视图侧 ----------------
    ProjectionSite(
        key="consolidation.mastery_view",
        target="backend/memory/graph/consolidation.py::_mastery_view",
        kind="payload",
        fields=_frontmatter_fields(),
        absent={
            "related_topic_keys": "视图把链接放在 `links` 键里（链接治理读它）",
            "search_text": "视图是 LLM 输入，不需要检索域",
        },
        extra_keys=frozenset(
            {
                "memory_id",
                "memory_type",
                "topic_key",
                "topic_title",
                "version",
                "overview",
                "understood",
                "difficulties",
                "review_advice",
                "links",
                "evidence_refs",
            }
        ),
    ),
    ProjectionSite(
        key="consolidation.learner_view",
        target="backend/memory/graph/consolidation.py::_learner_view",
        kind="payload",
        fields={"name": ("name", Source.TITLE), "aliases": ("aliases", Source.ALIASES)},
        absent={
            "description": "learner 视图不投影 description（LLM 只需要偏好/目标/计划）",
            "keywords": "consolidation 只治理 mastery 的 keywords",
            "related_topic_keys": "同 mastery_view：链接放在 `links` 键里",
            "search_text": "视图是 LLM 输入，不需要检索域",
        },
        extra_keys=frozenset(
            {"memory_id", "memory_type", "version", "preferences", "goals", "plans", "links"}
        ),
    ),
    ProjectionSite(
        key="consolidation.dangling_namespace",
        target="backend/memory/graph/consolidation.py::_govern_dangling_links",
        kind="payload",
        fields={"name": ("title", Source.PASSTHROUGH), "aliases": ("aliases", Source.PASSTHROUGH)},
        absent={
            "description": "链接命名空间只需要 memory_id/name/aliases 三级解析键（§3.2）",
            "keywords": "链接解析不使用 keywords",
            "related_topic_keys": "命名空间是解析输入，不含链接目标本身",
            "search_text": "命名空间不参与检索",
        },
        extra_keys=frozenset({"memory_id"}),
    ),
    _consumer_site(
        "consolidation.load_user_documents",
        "backend/memory/graph/consolidation.py::_load_user_documents",
        must_call=("_mastery_view", "_learner_view"),
        note="把文档投影成 consolidation 视图",
    ),
    # ---------------- 注册表读取侧（其余模块） ----------------
    ProjectionSite(
        key="summary.resolve_existing_memories",
        target="backend/memory/graph/summary.py::resolve_existing_memories",
        kind="read",
        fields={
            "name": ("title", Source.PASSTHROUGH),
            "description": ("summary", Source.PASSTHROUGH),
        },
        absent={
            "aliases": "汇总只用标题与摘要做候选匹配，不读别名列",
            "keywords": "同上：keywords 不参与这里的候选匹配",
            "related_topic_keys": "同上：链接列不参与候选匹配",
            "search_text": "同上：这里不走相似度域",
        },
        payload_keys=frozenset({"title", "summary"}),
    ),
    ProjectionSite(
        key="index_entries.search_candidates",
        target="backend/memory/persistence/index_entries.py::search_candidates",
        kind="read",
        fields=_registry_fields(),
        extra_keys=frozenset({"updated_at"}),
    ),
    ProjectionSite(
        key="dangling_links.link_namespace",
        target="backend/memory/persistence/dangling_links.py::link_namespace",
        kind="read",
        fields={"name": ("title", Source.PASSTHROUGH), "aliases": ("aliases", Source.PASSTHROUGH)},
        absent={
            "description": "链接命名空间只需要 memory_id/title/aliases 三级解析键（§3.2）",
            "keywords": "链接解析不使用 keywords",
            "related_topic_keys": "命名空间是解析输入，不含链接目标本身",
            "search_text": "命名空间不参与检索",
        },
        extra_keys=frozenset({"memory_id"}),
    ),
)

#: 发现规则命中、但**不是**投影站点的函数（必须写明理由，否则就是漏登记）。
DECLARED_NON_SITES: dict[str, str] = {
    "backend/memory/graph/consolidation.py::consolidate_user_memory": (
        "结果统计字典（keywords/aliases 是 applied 计数，不是投影字段）"
    ),
    "backend/memory/graph/consolidation.py::_dual_write_kg": (
        "KG 双写的**输入**载荷（changed_topics 的 keywords 是图谱证据，不是注册表投影）"
    ),
    "backend/memory/services/memory_service.py::MemoryService.forget": (
        "删除路径：按 (user_id, memory_id) 无条件 DELETE 投影行，不读写字段级投影"
        "（删除路径的全量枚举由本轮另一位负责人的元测试覆盖）"
    ),
}

#: 已登记、但 AST 发现规则**看不到**的站点，逐条写明原因。
MANUAL_SITES: dict[str, str] = {
    "backend/memory/graph/consolidation.py::_load_user_documents": (
        "锚点是 `_mastery_view`/`_learner_view`，而这两个名字在 api/memories.py 与 "
        "context_service.py 各有同名函数，作为全局锚点会大量误报，故手动登记"
    ),
    "backend/memory/persistence/dangling_links.py::link_namespace": (
        "纯函数：只接收已经取好的行（`.get('title')`/`.get('aliases')`），体内没有 SQL、"
        "也没有载荷字典，字典规则与 SQL 规则都够不着"
    ),
}

#: 投影的**只读消费站点**（本轮不纳入枚举，逐条写明原因与替代覆盖）。
#: 它们只读 PG 投影列、不写投影，字段漂移不会造成"投影不一致"这一类缺陷。
OUT_OF_SCOPE_READERS: dict[str, str] = {
    "backend/memory/services/memory_tools.py::build_search_sql": (
        "memory.search 的 SQL 构造（四列 ILIKE 匹配域）；字段清单由 test_memory_tools.py "
        "与检索集成测试覆盖"
    ),
    "backend/memory/services/memory_tools.py::fetch_index_projection": (
        "prime 目录读取：只 SELECT 已有投影列，不写投影"
    ),
    "backend/memory/services/search_service.py::SearchService.search": (
        "读 index_entries.search_candidates 的返回行做排序，不直接碰投影表"
    ),
    "backend/memory/services/context_service.py::assemble_context": (
        "会话上下文组装：消费 memory.search/prime 的结果，不写投影"
    ),
}

#: AST 发现规则的锚点符号：引用任何一个都说明这个函数在投影管线里。
ANCHOR_SYMBOLS: frozenset[str] = frozenset(
    {
        "index_projection_from_document",
        "projected_title",
        "_v2_front_matter",
        "_render_index_block",
        "_render_index_v2",
        "_render_index_item",
        "render_index",
        "_upsert_index_entry",
        "_index_projection",
        "_index_entry_from_projection",
        "_index_entry_from_block",
        "_parse_index_blocks",
        "render_mastery",
        "render_learner",
    }
)

#: brief 明确要求覆盖的站点（改名/删除必须显式改这里，防止"悄悄漏掉一个"）。
REQUIRED_SITE_TARGETS: tuple[str, ...] = (
    "backend/memory/storage/markdown_schema.py::_v2_front_matter",
    "backend/memory/storage/markdown_schema.py::_render_index_block",
    "backend/memory/storage/markdown_schema.py::_render_index_v2",
    "backend/memory/services/memory_service.py::MemoryService._build_new_content",
    "backend/memory/services/memory_service.py::MemoryService.restore",
    "backend/memory/services/memory_service.py::MemoryService.rebuild_index",
    "backend/memory/services/memory_service.py::MemoryService._index_projection",
    "backend/memory/services/memory_service.py::MemoryService._index_entry_from_projection",
)


def _site(key: str) -> ProjectionSite:
    for site in SITES:
        if site.key == key:
            return site
    raise AssertionError(f"未登记的站点 key: {key}")


# ---------------------------------------------------------------------------
# 4. AST 发现与结构断言
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FunctionInfo:
    path: str
    #: 完整限定名（含嵌套函数）
    qualname: str
    #: 最外层函数的限定名（嵌套函数归到它，避免 ``outer._inner`` 之类的假站点）
    root_qualname: str
    lineno: int
    node: ast.FunctionDef | ast.AsyncFunctionDef

    @property
    def target(self) -> str:
        return f"{self.path}::{self.qualname}"

    @property
    def root_target(self) -> str:
        return f"{self.path}::{self.root_qualname}"


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _collect_functions(
    node: ast.AST,
    path: str,
    *,
    classes: list[str],
    functions: list[str],
    root: str | None,
) -> list[_FunctionInfo]:
    out: list[_FunctionInfo] = []
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            out.extend(
                _collect_functions(
                    child, path, classes=[*classes, child.name], functions=[], root=None
                )
            )
        elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
            qualname = ".".join([*classes, *functions, child.name])
            this_root = root or qualname
            out.append(
                _FunctionInfo(
                    path=path,
                    qualname=qualname,
                    root_qualname=this_root,
                    lineno=child.lineno,
                    node=child,
                )
            )
            out.extend(
                _collect_functions(
                    child,
                    path,
                    classes=classes,
                    functions=[*functions, child.name],
                    root=this_root,
                )
            )
    return out


@lru_cache(maxsize=1)
def _scan_functions() -> tuple[_FunctionInfo, ...]:
    """扫 ``backend/memory/**`` 的全部函数（含类方法与嵌套函数）。"""
    found: list[_FunctionInfo] = []
    for path in sorted(MEMORY_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found.extend(_collect_functions(tree, _relative(path), classes=[], functions=[], root=None))
    return tuple(found)


def _dict_keys(info: _FunctionInfo) -> frozenset[str]:
    """函数体内所有 dict 字面量的字符串键并集。"""
    keys: set[str] = set()
    for node in ast.walk(info.node):
        if isinstance(node, ast.Dict):
            keys |= {
                key.value
                for key in node.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            }
    return frozenset(keys)


def _read_keys(info: _FunctionInfo) -> frozenset[str]:
    """``x.get("k")`` / ``x["k"]`` / ``getattr(x, "k")`` 读到的键。"""
    keys: set[str] = set()
    for node in ast.walk(info.node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
        ):
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                keys.add(first.value)
        elif isinstance(node, ast.Subscript):
            slice_node = node.slice
            if isinstance(slice_node, ast.Constant) and isinstance(slice_node.value, str):
                keys.add(slice_node.value)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) > 1
        ):
            second = node.args[1]
            if isinstance(second, ast.Constant) and isinstance(second.value, str):
                keys.add(second.value)
    return frozenset(keys)


def _sql_literals(info: _FunctionInfo) -> list[str]:
    """``text("...")`` 里的 SQL（只看真正送进数据库的字符串，不看 docstring）。"""
    out: list[str] = []
    for node in ast.walk(info.node):
        is_text_call = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "text"
        )
        if is_text_call:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    out.append(arg.value)
    return out


def _sql_keys(info: _FunctionInfo) -> frozenset[str]:
    """SQL 里出现的标识符（与投影字段拼写求交后才是"读到的投影列"）。"""
    keys: set[str] = set()
    for sql in _sql_literals(info):
        keys |= set(re.findall(r"[A-Za-z_][A-Za-z_0-9]*", sql))
    return frozenset(keys)


def _index_entry_kwargs(info: _FunctionInfo) -> frozenset[str]:
    """构造 ``IndexEntry(...)`` 时传的关键字名。"""
    keys: set[str] = set()
    for node in ast.walk(info.node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "IndexEntry"
        ):
            keys |= {kw.arg for kw in node.keywords if kw.arg}
    return frozenset(keys)


def _function_source(info: _FunctionInfo) -> str:
    """按 AST 行号从源文件取该函数源码（不 import 目标模块，避免循环依赖）。"""
    lines = (REPO_ROOT / info.path).read_text(encoding="utf-8").splitlines()
    assert info.node.end_lineno is not None
    return "\n".join(lines[info.node.lineno - 1 : info.node.end_lineno])


def _referenced_symbols(info: _FunctionInfo) -> frozenset[str]:
    names = {node.id for node in ast.walk(info.node) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(info.node) if isinstance(node, ast.Attribute)}
    return frozenset(names)


def _is_candidate(info: _FunctionInfo) -> bool:
    """发现规则（四条，互补）：

    1. 引用投影管线的权威符号（锚点）；
    2. 写了像投影载荷 / 注册表载荷的字典字面量；
    3. 构造 ``IndexEntry`` 且带 ≥2 个 index 条目字段（解析侧站点）；
    4. 跑了 ``text(...)`` SQL 且语句里出现 ``memory_index_entries``（落库/读回站点）。

    规则 3/4 是必需的：``_index_entry_from_block`` 与 ``_index_projection`` 不含任何锚点、
    也没有"载荷字典"，只靠规则 1/2 会漏。任何一条命中都必须登记。
    """
    if _referenced_symbols(info) & ANCHOR_SYMBOLS:
        return True
    keys = _dict_keys(info)
    if len(keys & PROJECTION_SPELLINGS) >= 3 or len(keys & REGISTRY_CORE_KEYS) >= 2:
        return True
    if len(_index_entry_kwargs(info) & INDEX_BLOCK_KEYS) >= 2:
        return True
    return any("memory_index_entries" in sql for sql in _sql_literals(info))


def _find_function(target: str) -> _FunctionInfo:
    for info in _scan_functions():
        if info.target == target:
            return info
    raise AssertionError(f"站点不存在（改名了？）：{target}")


def _extracted_keys(site: ProjectionSite, info: _FunctionInfo) -> frozenset[str]:
    """站点实际写/读的投影字段拼写（按站点性质选提取方式）。"""
    if site.kind == "payload":
        return _dict_keys(info) & PROJECTION_SPELLINGS
    if site.kind == "read":
        extracted = _read_keys(info) | _sql_keys(info) | _index_entry_kwargs(info)
        return extracted & PROJECTION_SPELLINGS
    if site.kind == "construct":
        return _index_entry_kwargs(info) & PROJECTION_SPELLINGS
    return frozenset()


# ---------------------------------------------------------------------------
# 5. 枚举与字段清单：新站点 / 新字段一定会失败
# ---------------------------------------------------------------------------


def test_required_sites_are_declared() -> None:
    """brief 要求覆盖的站点一个都不能少（改名/删除必须显式改清单）。"""
    declared = {site.target for site in SITES}
    missing = sorted(set(REQUIRED_SITE_TARGETS) - declared)
    assert missing == [], f"必需站点未登记：{missing}"


def test_every_declared_site_exists() -> None:
    for site in SITES:
        _find_function(site.target)


def test_no_undeclared_projection_site() -> None:
    """AST 自动枚举：发现规则命中的函数都必须是已登记站点。

    这是"**新增一个投影站点**"的失败哨兵：新写一个自己拼 index_data 的函数（或引用
    ``_v2_front_matter`` / ``_render_index_block`` / ``_upsert_index_entry`` 等管线符号的
    函数），本测试立刻报未登记。
    """
    declared = {site.target for site in SITES} | set(DECLARED_NON_SITES)
    discovered = {info.root_target for info in _scan_functions() if _is_candidate(info)}
    undeclared = sorted(discovered - declared)
    assert undeclared == [], (
        "发现未登记的投影站点：新站点必须在 SITES 里登记（说明它承载哪些字段、取值来源"
        "是什么）；若确认不是投影站点，登记到 DECLARED_NON_SITES 并写明理由。"
        f"未登记：{undeclared}"
    )


def test_declared_sites_are_actually_discovered() -> None:
    """反向：登记的站点必须真的能被发现规则看到，否则登记形同虚设。

    只有 ``MANUAL_SITES``（逐条写明"规则为什么够不着"）允许豁免。
    """
    discovered = {info.root_target for info in _scan_functions() if _is_candidate(info)}
    invisible = sorted(
        site.target
        for site in SITES
        if site.target not in discovered and site.target not in MANUAL_SITES
    )
    assert invisible == [], f"登记了但发现规则看不到（规则或站点失效）：{invisible}"


def test_manual_sites_have_documented_reasons() -> None:
    """手动登记豁免必须写明原因，且确实已登记为站点。"""
    declared = {site.target for site in SITES}
    for target, reason in MANUAL_SITES.items():
        assert target in declared, f"{target} 不在 SITES 里"
        assert len(reason.strip()) > 10, f"{target} 的豁免理由太短"


def test_out_of_scope_readers_have_documented_reasons() -> None:
    """只读消费站点清单必须逐条写明原因（否则就是"忘了枚举"而不是"有意不枚举"）。"""
    declared = {site.target for site in SITES}
    for target, reason in OUT_OF_SCOPE_READERS.items():
        assert len(reason.strip()) > 10, f"{target} 缺少理由"
        assert target not in declared


def test_non_sites_are_really_not_sites() -> None:
    """``DECLARED_NON_SITES`` 里必须都是被规则命中、但确实不写投影的函数。"""
    discovered = {info.root_target for info in _scan_functions() if _is_candidate(info)}
    for target, reason in DECLARED_NON_SITES.items():
        assert target in discovered, f"{target} 并未被规则命中，删掉这条豁免"
        assert reason.strip(), f"{target} 缺少豁免理由"


def test_every_site_accounts_for_every_projection_field() -> None:
    """每个站点都要么承载、要么写明"为什么不承载"某个投影字段。"""
    problems: list[str] = []
    for site in SITES:
        for canonical in PROJECTION_FIELDS:
            if canonical in site.fields:
                continue
            if not site.absent.get(canonical):
                problems.append(f"{site.key} 既没承载 {canonical}，也没写 absent 理由")
    assert problems == [], "\n".join(problems)


def test_projection_fields_doc_covers_the_schema() -> None:
    """``PROJECTION_FIELDS`` 说明表必须覆盖全部规范字段，且每个字段都有人承载。"""
    assert set(PROJECTION_FIELDS) == {
        "name",
        "description",
        "aliases",
        "keywords",
        "related_topic_keys",
    }
    for canonical in PROJECTION_FIELDS:
        assert any(canonical in site.fields for site in SITES), f"{canonical} 无人承载"
    for site in SITES:
        for canonical, (output_key, _source) in site.fields.items():
            assert output_key in PROJECTION_SPELLINGS, (
                f"{site.key} 的 {canonical} → 未知输出键 {output_key}"
            )


def test_registry_payload_keys_match_the_single_implementation() -> None:
    """唯一权威实现实际产出的键集合 == 清单（**新增字段会在这里失败**）。"""
    actual = index_projection_from_document(PROBE.mastery.doc)
    assert set(actual) == REGISTRY_PAYLOAD_KEYS, (
        "index_projection_from_document 的键集合变了："
        f"实际 {sorted(actual)}，清单 {sorted(REGISTRY_PAYLOAD_KEYS)}。"
        "新增投影字段请同时更新：本清单、SITES 各站点、_upsert_index_entry 的 SQL、"
        "index 渲染/解析，以及（如需要）Alembic 迁移。"
    )


def test_index_block_field_order_matches_the_schema() -> None:
    """``_V2_INDEX_FIELDS``（渲染与解析共用的字段顺序）必须与清单一致。"""
    assert set(schema._V2_INDEX_FIELDS) == INDEX_BLOCK_KEYS


def test_site_key_inventory_is_exact() -> None:
    """**核心字段清单断言**：站点实际写/读的键集合必须与登记**恰好相等**。"""
    problems: list[str] = []
    for site in SITES:
        if site.kind not in {"payload", "read", "construct"}:
            continue
        extracted = _extracted_keys(site, _find_function(site.target))
        if extracted != site.expected_spellings:
            problems.append(
                f"{site.key}（{site.kind}）：\n"
                f"    提取到 {sorted(extracted)}\n"
                f"    登记为 {sorted(site.expected_spellings)}\n"
                f"    多出 {sorted(extracted - site.expected_spellings)} /"
                f" 缺少 {sorted(site.expected_spellings - extracted)}"
            )
    assert problems == [], (
        "站点字段清单与实现不一致（新增/删除投影字段必须同时改两边）：\n" + "\n".join(problems)
    )


def test_pg_projection_columns_are_written_and_read_consistently() -> None:
    """PG 列清单：写入（INSERT + UPDATE）与读取（SELECT）两侧必须覆盖各自清单。"""
    writer = _find_function(
        "backend/memory/services/memory_service.py::MemoryService._upsert_index_entry"
    )
    reader = _find_function(
        "backend/memory/services/memory_service.py::MemoryService._index_projection"
    )
    joined_write = "\n".join(_sql_literals(writer))
    joined_read = "\n".join(_sql_literals(reader))
    insert_match = re.search(r"INSERT INTO memory_index_entries\s*\(([^)]*)\)", joined_write, re.S)
    assert insert_match is not None, "找不到 INSERT 列清单"
    insert_columns = {item.strip() for item in insert_match.group(1).split(",") if item.strip()}
    update_columns = set(re.findall(r"(\w+)\s*=\s*EXCLUDED\.\w+", joined_write))
    select_match = re.search(r"SELECT\s+(.*?)\s+FROM memory_index_entries", joined_read, re.S)
    assert select_match is not None, "找不到 SELECT 列清单"
    select_columns = {item.strip() for item in select_match.group(1).split(",") if item.strip()}

    for column in sorted(PG_PROJECTION_COLUMNS):
        assert column in insert_columns, f"INSERT 少了投影列 {column}"
        assert column in update_columns, f"UPDATE ... EXCLUDED 少了投影列 {column}"
    for column in sorted(PG_INDEX_READ_COLUMNS | {"updated_at"}):
        assert column in select_columns, f"SELECT 少了投影列 {column}"
    assert (
        insert_columns
        - {
            "user_id",
            "memory_id",
            "source_version",
            "memory_type",
            "topic_key",
            "evidence_refs",
            "updated_at",
        }
        == PG_PROJECTION_COLUMNS
    )


def test_delegate_and_consumer_sites_call_the_pipeline() -> None:
    """delegate/consumer 站点必须真的调用管线符号（不是自己又拼一份等价逻辑）。"""
    for site in SITES:
        if not site.must_call:
            continue
        symbols = _referenced_symbols(_find_function(site.target))
        missing = sorted(set(site.must_call) - symbols)
        assert missing == [], f"{site.key} 没有调用 {missing}（投影逻辑又分叉了？）"


def test_delegate_and_consumer_sites_do_not_rebuild_the_registry_payload() -> None:
    """透传/分派站点体内不得再出现注册表载荷字典（只能来自权威实现）。"""
    for site in SITES:
        if site.kind not in {"delegate", "consumer"}:
            continue
        keys = _dict_keys(_find_function(site.target))
        overlap = keys & REGISTRY_CORE_KEYS
        extra = keys - site.extra_keys
        assert not (len(overlap) >= 2 and len(extra) >= 3), (
            f"{site.key} 又开始自己拼投影载荷了：{sorted(keys)}；"
            "必须调用 index_projection_from_document。"
        )


def test_field_inventory_check_has_teeth() -> None:
    """自检：清单对比真的会因"多一个键 / 少一个键 / 换来源"失败（不是空转）。"""
    site = _site("schema.v2_front_matter")
    probe = PROBE.mastery
    ok = _expected_payload(site, probe)
    _assert_payload(
        site,
        {**ok, "aliases": list(probe.aliases), "keywords": list(probe.keywords)},
        probe,
    )

    with pytest.raises(AssertionError, match="输出键集合与登记不符"):
        _assert_payload(site, {**ok, "sources": ["x"]}, probe)
    with pytest.raises(AssertionError, match="输出键集合与登记不符"):
        _assert_payload(site, {k: v for k, v in ok.items() if k != "name"}, probe)
    with pytest.raises(AssertionError, match="取值来源错了"):
        _assert_payload(site, {**ok, "name": "换成了别的来源"}, probe)


def test_discovery_rule_has_teeth() -> None:
    """自检：发现规则确实在全仓扫描（不是空转）。"""
    discovered = {info.root_target for info in _scan_functions() if _is_candidate(info)}
    assert len(discovered) > 10
    assert "backend/memory/storage/markdown_schema.py::_v2_front_matter" in discovered
    declared = {site.target for site in SITES}
    assert declared <= discovered | set(DECLARED_NON_SITES) | set(MANUAL_SITES)


def test_site_registry_has_no_duplicates() -> None:
    keys = [site.key for site in SITES]
    assert len(keys) == len(set(keys))
    targets = [site.target for site in SITES]
    assert len(targets) == len(set(targets))


def test_function_source_is_readable_for_every_site() -> None:
    """``_function_source`` 对所有站点可用（AST 断言的前提）。"""
    for site in SITES:
        assert _function_source(_find_function(site.target)).strip(), site.key


# ---------------------------------------------------------------------------
# 6. 可调用站点：喂探针 → 比对真值表
# ---------------------------------------------------------------------------


class _FakeSession:
    """最小 AsyncSession 替身：投影站点只用它开事务 / 执行 SQL。"""

    def __init__(self) -> None:
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def begin(self) -> _FakeSession:
        return self

    async def execute(self, statement: Any, params: Any = None) -> Any:
        self.executed.append((str(statement), dict(params or {})))
        return None


@dataclass(frozen=True)
class _Stored:
    storage_key: str
    checksum: str


class _FakeStore:
    """MarkdownStore 替身：只需版本读写与 current 物化三个方法。"""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.written: list[bytes] = []
        self.materialized: list[bytes] = []

    async def read_version_by_id(self, **kwargs: Any) -> bytes:
        return self.content

    async def read_version(self, **kwargs: Any) -> bytes:
        return self.content

    async def write_immutable_version(self, **kwargs: Any) -> _Stored:
        content = kwargs["content"]
        self.written.append(content)
        return _Stored(
            storage_key=f"versions/{kwargs['memory_id']}.md",
            checksum=hashlib.sha256(content).hexdigest(),
        )

    async def materialize_current(self, **kwargs: Any) -> None:
        self.materialized.append(kwargs["content"])


def _settings() -> Settings:
    return Settings(_env_file=None, app_env="test", memory_storage_root="/tmp/probe-storage")


def _service(store: Any) -> MemoryService:
    return MemoryService(settings=_settings(), session_factory=_FakeSession, store=store)


def _memory_id(doc_probe: DocProbe) -> str:
    if doc_probe.memory_type == "learner":
        return "learner"
    return f"mastery:{doc_probe.doc.topic_key}"


def _rendered(doc_probe: DocProbe) -> bytes:
    if doc_probe.memory_type == "learner":
        return schema.render_learner(doc_probe.doc).encode("utf-8")
    return schema.render_mastery(doc_probe.doc).encode("utf-8")


def _patch_active_document(
    monkeypatch: pytest.MonkeyPatch, doc_probe: DocProbe, *, version: int = 2
) -> None:
    async def fake_active(
        self: MemoryService, session: Any, *, user_id: UUID, memory_id: str
    ) -> tuple[dict[str, Any], Any]:
        row = {
            "user_id": user_id,
            "memory_id": memory_id,
            "memory_type": doc_probe.memory_type,
            "topic_key": getattr(doc_probe.doc, "topic_key", None),
            "topic_title": doc_probe.topic_title,
            "active_version": version,
            "active_storage_key": "versions/x.md",
            "active_checksum": "0" * 64,
            "deleted_at": None,
            "index_dirty_at": None,
            "updated_at": NOW,
        }
        return row, doc_probe.doc

    monkeypatch.setattr(MemoryService, "_load_active_document", fake_active)


def _capturing_upsert(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def fake_upsert(self: MemoryService, session: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(MemoryService, "_upsert_index_entry", fake_upsert)
    return captured


def _noop_dirty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_dirty(session: Any, *, user_id: UUID, dirty_at: datetime) -> None:
        return None

    monkeypatch.setattr(ms_module.docs_repo, "mark_index_dirty", fake_dirty)


def _patch_restore_dependencies(
    monkeypatch: pytest.MonkeyPatch, doc_probe: DocProbe, content: bytes
) -> dict[str, Any]:
    """把 restore 的外部依赖换掉，只保留"解析文档 → 组装投影"这条被测路径。"""
    row = {
        "user_id": USER_ID,
        "memory_id": _memory_id(doc_probe),
        "memory_type": doc_probe.memory_type,
        "topic_key": getattr(doc_probe.doc, "topic_key", None),
        "topic_title": doc_probe.topic_title,
        "deleted_at": NOW,
        "deleted_version": 2,
        "tombstone_until": datetime(2030, 1, 1, tzinfo=UTC),
        "active_version": None,
    }

    async def fake_lock(session: Any, *, user_id: UUID, memory_ids: list[str]) -> list[Any]:
        return [row]

    async def fake_commit(
        self: MemoryService, session: Any, *, user_id: UUID, memory_id: str, version: int
    ) -> dict[str, Any]:
        return {"checksum": hashlib.sha256(content).hexdigest()}

    async def fake_max_version(session: Any, *, user_id: UUID, memory_id: str) -> int:
        return 2

    async def fake_lock_user(session: Any, user_id: UUID) -> None:
        return None

    async def fake_noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ms_module, "acquire_user_lock", fake_lock_user)
    monkeypatch.setattr(ms_module.docs_repo, "lock_documents", fake_lock)
    monkeypatch.setattr(ms_module.docs_repo, "restore_document", fake_noop)
    monkeypatch.setattr(ms_module.docs_repo, "mark_index_dirty", fake_noop)
    monkeypatch.setattr(ms_module.docs_repo, "get_max_version", fake_max_version)
    monkeypatch.setattr(ms_module.commits_repo, "get_by_mutation_id", fake_noop)
    monkeypatch.setattr(ms_module.commits_repo, "insert_commit", fake_noop)
    monkeypatch.setattr(ms_module.outbox_repo, "insert_event", fake_noop)
    monkeypatch.setattr(MemoryService, "_find_commit_for_version", fake_commit)
    return _capturing_upsert(monkeypatch)


# ---- 同步站点 ----


def test_v2_front_matter_carries_the_registered_fields() -> None:
    site = _site("schema.v2_front_matter")
    for doc_probe in (PROBE.mastery, PROBE.learner):
        _assert_payload(site, schema._v2_front_matter(doc_probe.doc), doc_probe)


def test_v1_front_matter_stays_v1() -> None:
    """§5.6：v1 历史文件读进来再写回去必须仍是 v1（不得补出 v2 字段）。"""
    assert schema._v2_front_matter(PROBE.mastery_v1.doc) == {}
    rendered = schema.render_mastery(PROBE.mastery_v1.doc)
    assert "name:" not in rendered
    assert "schema_version: 1" in rendered


def test_render_mastery_and_learner_embed_the_same_v2_fields() -> None:
    for key, doc_probe in (
        ("schema.render_mastery", PROBE.mastery),
        ("schema.render_learner", PROBE.learner),
    ):
        site = _site(key)
        rendered = (
            schema.render_mastery(doc_probe.doc)
            if doc_probe.memory_type == "mastery"
            else schema.render_learner(doc_probe.doc)
        )
        fields, _body = schema.parse_front_matter(rendered)
        actual = {output_key: fields[output_key] for _c, (output_key, _s) in site.fields.items()}
        _assert_payload(site, actual, doc_probe)
        if doc_probe.topic_title is not None:
            # §3.2：topic_key / topic_title 与 v2 的 name 并存
            assert fields["topic_title"] == doc_probe.topic_title


def _parse_block_lines(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        if line.startswith("### ") or not line.startswith("- "):
            continue
        key, _, value = line[2:].partition(":")
        values[key.strip()] = value.strip()
    return values


def _index_entry(doc_probe: DocProbe, *, version: int = 7) -> schema.IndexEntry:
    return schema.IndexEntry(
        memory_id=_memory_id(doc_probe),
        memory_type=doc_probe.memory_type,
        topic_key=getattr(doc_probe.doc, "topic_key", None),
        title=str(doc_probe.name or ""),
        version=version,
        updated_at=NOW,
        description=doc_probe.body_summary,
        aliases=list(doc_probe.aliases),
        related_topic_keys=list(doc_probe.links),
        keywords=list(doc_probe.keywords),
    )


def test_render_index_block_is_pure_passthrough() -> None:
    """index 块渲染只允许原样搬运条目字段（不许换来源、不许漏字段）。"""
    site = _site("schema.render_index_block")
    actual = _parse_block_lines(schema._render_index_block(_index_entry(PROBE.mastery)))
    assert actual["name"] == PROBE.mastery.name
    assert actual["description"] == PROBE.mastery.body_summary
    assert actual["aliases"] == " | ".join(PROBE.mastery.aliases)
    assert actual["keywords"] == " | ".join(PROBE.mastery.keywords)
    assert actual["related"] == " | ".join(PROBE.mastery.links)
    assert actual["version"] == "v7"
    assert set(actual) == site.expected_keys


def test_render_index_v2_round_trips_through_the_real_parser() -> None:
    """index.md 渲染 → 解析回来必须拿到同一份投影（解析侧与渲染侧同源）。"""
    site = _site("schema.render_index_v2")
    document = schema.IndexDocument(
        user_id=USER_ID,
        version=5,
        updated_at=NOW,
        schema_version=schema.SCHEMA_VERSION_V2,
        learner=_index_entry(PROBE.learner, version=2),
        mastery_entries=[_index_entry(PROBE.mastery)],
    )
    parsed = schema.parse_index(schema._render_index_v2(document))
    assert parsed.schema_version == schema.SCHEMA_VERSION_V2
    assert parsed.learner is not None
    assert len(parsed.mastery_entries) == 1
    entry = parsed.mastery_entries[0]
    actual = {
        "name": entry.title,
        "description": entry.description,
        "aliases": entry.aliases,
        "keywords": entry.keywords,
        "related": entry.related_topic_keys,
        "version": f"v{entry.version}",
        "updated_at": entry.updated_at.isoformat(),
    }
    assert set(actual) == site.expected_keys
    assert entry.title == PROBE.mastery.name
    assert entry.description == PROBE.mastery.body_summary
    assert entry.aliases == PROBE.mastery.aliases
    assert entry.keywords == PROBE.mastery.keywords
    assert entry.related_topic_keys == PROBE.mastery.links
    assert entry.updated_at == NOW
    assert parsed.learner.title == PROBE.learner.name


def test_mastery_view_uses_the_projected_title() -> None:
    """consolidation 的 mastery 视图是第五个标题站点，必须走 projected_title。"""
    from backend.memory.graph import consolidation

    site = _site("consolidation.mastery_view")
    actual = consolidation._mastery_view(PROBE.mastery.doc)
    assert actual["name"] == PROBE.mastery.name
    assert actual["description"] == PROBE.mastery.description
    assert actual["aliases"] == PROBE.mastery.aliases
    assert actual["keywords"] == PROBE.mastery.keywords
    assert set(actual) == site.expected_keys
    # 反向护栏：改掉 name，视图标题必须跟着变（旧实现读的是 topic_title）
    mutated = _copy_mastery(PROBE.mastery.doc, name="PROBE-RENAMED")
    assert consolidation._mastery_view(mutated)["name"] == "PROBE-RENAMED"


def test_learner_view_uses_the_projected_title() -> None:
    from backend.memory.graph import consolidation

    site = _site("consolidation.learner_view")
    actual = consolidation._learner_view(PROBE.learner.doc)
    assert actual["name"] == PROBE.learner.name
    assert actual["aliases"] == PROBE.learner.aliases
    assert set(actual) == site.expected_keys


def test_dangling_namespace_reads_title_and_aliases() -> None:
    """链接命名空间（三级解析：memory_id → name → aliases）读投影标题与别名。"""
    site = _site("consolidation.dangling_namespace")
    info = _find_function(site.target)
    assert _extracted_keys(site, info) == site.expected_spellings
    assert "name" in _read_keys(info), "命名空间必须读投影的 name（§3.2 解析优先级）"


def _copy_mastery(doc: schema.MasteryDocument, **updates: Any) -> schema.MasteryDocument:
    payload = {name: getattr(doc, name) for name in schema.MasteryDocument.__dataclass_fields__}
    payload.update(updates)
    return schema.MasteryDocument(**payload)


# ---- 注册表写入侧（异步） ----


async def test_projection_impl_matches_the_truth_table() -> None:
    site = _site("ms.projection_impl")
    for doc_probe in (PROBE.mastery, PROBE.learner, PROBE.mastery_v1):
        _assert_payload(site, index_projection_from_document(doc_probe.doc), doc_probe)


async def test_build_new_content_matches_the_single_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = _site("ms.build_new_content")
    for doc_probe in (PROBE.mastery, PROBE.learner):
        _patch_active_document(monkeypatch, doc_probe)
        plan = CommitMutationPlan(
            mutation_id=uuid4(),
            memory_id=_memory_id(doc_probe),
            target_memory_type=doc_probe.memory_type,  # type: ignore[arg-type]
            topic_title=doc_probe.topic_title,
            action="merge",
            expected_version=2,
            learner_patch=LearnerPatch() if doc_probe.memory_type == "learner" else None,
            mastery_patch=MasteryPatch() if doc_probe.memory_type == "mastery" else None,
        )
        _content, _bv, _av, _tk, index_data = await _service(_FakeStore(b""))._build_new_content(
            _FakeSession(), user_id=USER_ID, plan=plan, now=NOW
        )
        # 提交路径的 links 从**新渲染正文**现算（I-11②），与探针登记一致
        expected = index_projection_from_document(
            doc_probe.doc, related_topic_keys=list(doc_probe.links)
        )
        payload = {k: v for k, v in index_data.items() if k != "changed_sections"}
        _assert_payload(site, payload, doc_probe)
        assert payload == expected
        # learner 分支额外产出 changed_sections（learner.updated 事件载荷），不是投影字段
        assert set(index_data) - set(payload) <= {"changed_sections"}
        if doc_probe.memory_type == "learner":
            assert index_data["changed_sections"] == []


async def test_restore_matches_the_single_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新发现 7 的主战场：恢复路径曾自己拼投影、title 取 topic_title。"""
    site = _site("ms.restore")
    for doc_probe in (PROBE.mastery, PROBE.learner):
        content = _rendered(doc_probe)
        captured = _patch_restore_dependencies(monkeypatch, doc_probe, content)
        store = _FakeStore(content)
        outcome = await _service(store).restore(
            operation_id=uuid4(),
            user_id=USER_ID,
            actor_type="user",
            mutation_id=uuid4(),
            memory_id=_memory_id(doc_probe),
            deleted_version=2,
        )
        assert outcome.after_version == 3
        payload = captured["index_data"]
        assert payload == index_projection_from_document(doc_probe.doc)
        _assert_payload(site, payload, doc_probe)
        assert store.materialized, "恢复后必须物化 current/"


async def test_refresh_index_projection_matches_the_single_implementation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    site = _site("ms.refresh_index_projection")
    for doc_probe in (PROBE.mastery, PROBE.learner):
        _patch_active_document(monkeypatch, doc_probe)
        _noop_dirty(monkeypatch)
        captured = _capturing_upsert(monkeypatch)
        refreshed = await _service(_FakeStore(b"")).refresh_index_projection(
            _FakeSession(), user_id=USER_ID, memory_id=_memory_id(doc_probe)
        )
        assert refreshed is True
        payload = captured["index_data"]
        assert payload == index_projection_from_document(doc_probe.doc)
        _assert_payload(site, payload, doc_probe)


async def test_all_write_sites_produce_the_same_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**核心一致性断言**：提交 / 恢复 / 迁移刷新三条路径同一文档必须产出同一份载荷。"""
    for doc_probe in (PROBE.mastery, PROBE.learner):
        content = _rendered(doc_probe)
        memory_id = _memory_id(doc_probe)
        canonical = index_projection_from_document(doc_probe.doc)

        # 1) 提交路径
        _patch_active_document(monkeypatch, doc_probe)
        plan = CommitMutationPlan(
            mutation_id=uuid4(),
            memory_id=memory_id,
            target_memory_type=doc_probe.memory_type,  # type: ignore[arg-type]
            topic_title=doc_probe.topic_title,
            action="merge",
            expected_version=2,
            learner_patch=LearnerPatch() if doc_probe.memory_type == "learner" else None,
            mastery_patch=MasteryPatch() if doc_probe.memory_type == "mastery" else None,
        )
        _c, _b, _a, _t, build_payload = await _service(_FakeStore(content))._build_new_content(
            _FakeSession(), user_id=USER_ID, plan=plan, now=NOW
        )

        # 2) 迁移刷新路径
        _noop_dirty(monkeypatch)
        captured_refresh = _capturing_upsert(monkeypatch)
        await _service(_FakeStore(content)).refresh_index_projection(
            _FakeSession(), user_id=USER_ID, memory_id=memory_id
        )

        # 3) 恢复路径
        captured_restore = _patch_restore_dependencies(monkeypatch, doc_probe, content)
        await _service(_FakeStore(content)).restore(
            operation_id=uuid4(),
            user_id=USER_ID,
            actor_type="user",
            mutation_id=uuid4(),
            memory_id=memory_id,
            deleted_version=2,
        )

        # changed_sections 只属于 learner 提交路径（事件载荷），比对投影前先剥离
        build_projection = {k: v for k, v in build_payload.items() if k != "changed_sections"}
        assert build_projection == canonical
        assert captured_refresh["index_data"] == canonical
        assert captured_restore["index_data"] == canonical
        # 三条路径的 title 都必须是 frontmatter name（不是 topic_title）
        titles = {
            build_projection["title"],
            captured_refresh["index_data"]["title"],
            captured_restore["index_data"]["title"],
        }
        assert titles == {doc_probe.title}
        if doc_probe.topic_title is not None:
            assert doc_probe.title != doc_probe.topic_title, "探针必须让两来源可区分"


async def test_upsert_index_entry_passes_every_projection_column() -> None:
    site = _site("ms.upsert_index_entry")
    payload = index_projection_from_document(PROBE.mastery.doc)
    session = _FakeSession()
    await _service(_FakeStore(b""))._upsert_index_entry(
        session,
        user_id=USER_ID,
        memory_id="mastery:probe",
        memory_type="mastery",
        topic_key="probe",
        source_version=9,
        index_data=payload,
        evidence_refs=["evidence-1"],
        now=NOW,
    )
    assert session.executed, "必须真的执行一次 upsert"
    _sql, params = session.executed[-1]
    assert set(site.expected_keys) <= set(params), (
        f"落库参数缺少投影键：{sorted(set(site.expected_keys) - set(params))}"
    )
    for canonical, (output_key, source) in site.fields.items():
        if source is Source.PASSTHROUGH:
            continue
        assert params[output_key] == PROBE.mastery.source_value(source, canonical)
    assert params["source_version"] == 9
    assert str(params["user_id"]) == str(USER_ID)


def test_index_entry_from_projection_prefers_the_projection_title() -> None:
    """新发现 7：index.md 的 title 必须取投影行，而不是 memory_documents.topic_title。"""
    site = _site("ms.index_entry_from_projection")
    payload = index_projection_from_document(PROBE.mastery.doc)
    doc_row = {
        "memory_id": "mastery:probe",
        "memory_type": "mastery",
        "topic_key": "probe",
        "topic_title": "PROBE-TOPIC-TITLE-b21c",
        "active_version": 3,
        "updated_at": NOW,
    }
    entry = MemoryService._index_entry_from_projection(doc_row, payload)
    assert entry.title == payload["title"] == PROBE.mastery.name
    assert entry.title != doc_row["topic_title"]
    actual = {
        "title": entry.title,
        "description": entry.description,
        "aliases": entry.aliases,
        "keywords": entry.keywords,
        "related_topic_keys": entry.related_topic_keys,
        "version": entry.version,
        "updated_at": entry.updated_at,
        # 读回站点还会 `.get("summary")` 取注册表摘要列（拼写与 index 键不同）
        "summary": payload["summary"],
    }
    assert set(actual) == site.expected_keys
    # 透传：值与投影行逐键相等
    assert actual["description"] == payload["summary"]
    assert actual["aliases"] == payload["aliases"]
    assert actual["keywords"] == payload["keywords"]
    assert actual["related_topic_keys"] == payload["related_topic_keys"]


def test_index_entry_from_projection_falls_back_for_legacy_rows() -> None:
    doc_row = {
        "memory_id": "mastery:probe",
        "memory_type": "mastery",
        "topic_key": "probe",
        "topic_title": "PROBE-LEGACY-TITLE",
        "active_version": 1,
        "updated_at": NOW,
    }
    entry = MemoryService._index_entry_from_projection(doc_row, None)
    assert entry.title == "PROBE-LEGACY-TITLE"
    assert entry.aliases == [] and entry.keywords == []

    learner_row = {
        **doc_row,
        "memory_id": "learner",
        "memory_type": "learner",
        "topic_key": None,
        "topic_title": None,
    }
    assert (
        MemoryService._index_entry_from_projection(learner_row, None).title == LEARNER_PROFILE_TITLE
    )


def _rebuild_index_row(doc_probe: DocProbe) -> dict[str, Any]:
    return {
        "user_id": USER_ID,
        "memory_id": _memory_id(doc_probe),
        "memory_type": doc_probe.memory_type,
        "topic_key": getattr(doc_probe.doc, "topic_key", None),
        # 刻意与投影 title 不同：旧实现取的就是这一列，会与注册表不一致
        "topic_title": doc_probe.topic_title or "PROBE-LEARNER-TOPIC-TITLE",
        "active_version": 3,
        "deleted_at": None,
        "updated_at": NOW,
        "active_storage_key": "versions/x.md",
        "index_dirty_at": NOW,
    }


async def _run_rebuild(
    monkeypatch: pytest.MonkeyPatch,
    *,
    doc_row: dict[str, Any],
    projection: dict[str, Any] | None,
    store: _FakeStore,
) -> tuple[bytes, dict[str, Any]]:
    index_row = {"active_version": 1, "index_dirty_at": datetime(2026, 9, 12, 4, 0, tzinfo=UTC)}

    async def fake_get_document(
        session: Any, *, user_id: UUID, memory_id: str
    ) -> dict[str, Any] | None:
        return index_row if memory_id == "index" else doc_row

    async def fake_list_active(session: Any, *, user_id: UUID) -> list[dict[str, Any]]:
        return [doc_row]

    async def fake_projection(
        self: MemoryService, session: Any, *, user_id: UUID, memory_id: str
    ) -> dict[str, Any] | None:
        return projection

    async def fake_candidates(self: MemoryService, session: Any, *, user_id: UUID) -> list[str]:
        return []

    async def fake_lock_user(session: Any, user_id: UUID) -> None:
        return None

    async def fake_noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def fake_clear_dirty(session: Any, *, user_id: UUID, expected_dirty_at: Any) -> bool:
        return True

    monkeypatch.setattr(ms_module, "acquire_user_lock", fake_lock_user)
    monkeypatch.setattr(ms_module.docs_repo, "get_document", fake_get_document)
    monkeypatch.setattr(ms_module.docs_repo, "list_active_documents", fake_list_active)
    monkeypatch.setattr(ms_module.docs_repo, "set_active_version", fake_noop)
    monkeypatch.setattr(ms_module.docs_repo, "clear_index_dirty", fake_clear_dirty)
    monkeypatch.setattr(ms_module.commits_repo, "insert_commit", fake_noop)
    monkeypatch.setattr(MemoryService, "_index_projection", fake_projection)
    monkeypatch.setattr(MemoryService, "_candidate_topic_labels", fake_candidates)
    rebuilt = await _service(store).rebuild_index(user_id=USER_ID, operation_id=uuid4())
    assert store.written, "重建必须写出新的 index 版本"
    return store.written[-1], rebuilt


async def test_rebuild_index_matches_registry_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """index.md 重建必须与注册表投影一致（新发现 7 的第二处）。"""
    payload = index_projection_from_document(PROBE.mastery.doc)
    content, rebuilt = await _run_rebuild(
        monkeypatch,
        doc_row=_rebuild_index_row(PROBE.mastery),
        projection=payload,
        store=_FakeStore(b""),
    )
    assert rebuilt["rebuilt"] is True
    parsed = schema.parse_index(content.decode("utf-8"))
    assert len(parsed.mastery_entries) == 1
    entry = parsed.mastery_entries[0]
    assert entry.title == payload["title"] == PROBE.mastery.name
    assert entry.title != "PROBE-TOPIC-TITLE-b21c"
    assert entry.description == payload["summary"]
    assert entry.aliases == payload["aliases"]
    assert entry.keywords == payload["keywords"]
    assert entry.related_topic_keys == payload["related_topic_keys"]


async def test_rebuild_index_falls_back_when_projection_row_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """投影行缺失（历史数据未回填）时退回文档行标题，重建不能失败。"""
    doc_row = {**_rebuild_index_row(PROBE.mastery), "topic_title": "PROBE-FALLBACK-TITLE"}
    content, rebuilt = await _run_rebuild(
        monkeypatch, doc_row=doc_row, projection=None, store=_FakeStore(b"")
    )
    assert rebuilt["rebuilt"] is True
    entry = schema.parse_index(content.decode("utf-8")).mastery_entries[0]
    assert entry.title == "PROBE-FALLBACK-TITLE"
    assert entry.aliases == []
    assert entry.keywords == []


# ---- v1 与标题规则 ----


def test_v1_documents_project_topic_title_and_empty_v2_fields() -> None:
    probe = PROBE.mastery_v1
    payload = index_projection_from_document(probe.doc)
    assert payload["title"] == probe.topic_title
    assert payload["aliases"] == []
    assert payload["keywords"] == []
    assert payload["related_topic_keys"] == []
    assert payload["summary"] == probe.body_summary
    assert projected_title(probe.doc) == probe.topic_title


def test_projected_title_prefers_v2_name_and_falls_back() -> None:
    assert projected_title(PROBE.mastery.doc) == PROBE.mastery.name
    assert projected_title(PROBE.learner.doc) == PROBE.learner.name
    assert projected_title(_copy_mastery(PROBE.mastery.doc, name="")) == PROBE.mastery.topic_title
    assert projected_title(_copy_mastery(PROBE.mastery.doc, name=None)) == (
        PROBE.mastery.topic_title
    )
    empty_learner = schema.LearnerDocument(
        user_id=USER_ID,
        version=1,
        updated_at=NOW,
        schema_version=schema.SCHEMA_VERSION_V2,
        name="  ",
    )
    assert projected_title(empty_learner) == LEARNER_PROFILE_TITLE


def test_search_text_starts_with_the_projected_title() -> None:
    """search_text 也必须用投影标题开头（否则 v2 的 name 进不了相似度召回）。"""
    for doc_probe in (PROBE.mastery, PROBE.learner):
        payload = index_projection_from_document(doc_probe.doc)
        assert payload["search_text"].startswith(doc_probe.title)
        for term in doc_probe.aliases:
            assert term in payload["search_text"]
        for term in doc_probe.body_terms:
            assert term in payload["search_text"]
        if doc_probe.topic_title is not None:
            assert doc_probe.title != doc_probe.topic_title, "探针必须让两来源可区分"
