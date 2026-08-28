"""知识总结的确定性规范化、哈希与搜索文本构造（知识总结方案 §8.4、§11.1）。

本模块不访问数据库或模型服务。标题、条目、引用短文和当前 content 的序列化都在此
统一处理，以保证 API、Worker、编辑服务和运维任务使用同一版本化规则。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from hashlib import sha256
from typing import Any
from uuid import UUID

from backend.conversation.contracts.knowledge_summary import KnowledgeSummaryContent

KNOWLEDGE_CANONICAL_VERSION = "knowledge_canonical_v1"
KNOWLEDGE_CONTENT_SCHEMA_VERSION = 1
KNOWLEDGE_SECTIONS = (
    "definitions",
    "theorems",
    "formulas",
    "properties",
    "methods",
    "pitfalls",
)
ALL_KNOWLEDGE_SECTIONS = ("overview", *KNOWLEDGE_SECTIONS)

_TITLE_DELIMITERS = frozenset(
    {
        "\u3000",
        ":",
        "：",
        "-",
        "－",
        "—",
        "–",
        "·",
        "・",
        "/",
        "／",
        "|",
        "｜",
        ",",
        "，",
        ";",
        "；",
    }
)
_OUTER_WRAPPERS = (
    ("《", "》"),
    ("「", "」"),
    ("“", "”"),
    ("(", ")"),
    ("（", "）"),
    ("[", "]"),
    ("【", "】"),
)


def canonicalize_title_v1(value: str, *, max_length: int) -> str:
    """按 §11.1 生成标题或大主题的版本化规范化值。"""
    text = unicodedata.normalize("NFC", value)
    text = "".join(
        chr(ord(character) - 0xFEE0) if "\uff01" <= character <= "\uff5e" else character
        for character in text
    )
    text = "".join(" " if character in _TITLE_DELIMITERS else character for character in text)
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"[A-Z]", lambda match: match.group(0).lower(), text)
    for opening, closing in _OUTER_WRAPPERS:
        if len(text) >= 2 and text.startswith(opening) and text.endswith(closing):
            text = text[len(opening) : -len(closing)].strip()
            break
    if not text:
        raise ValueError("规范化标题不能为空")
    if len(text) > max_length:
        raise ValueError("规范化标题超过字段上限")
    return text


def canonicalize_item_text_v1(value: str) -> str:
    """按 §11.1 规范化知识条目文本，用于精确重复判断。"""
    text = unicodedata.normalize("NFC", value)
    text = re.sub(r"[\n\t ]+", " ", text).strip()
    text = re.sub(r"[。．.]+$", ".", text)
    if not text:
        raise ValueError("规范化条目不能为空")
    return text


def canonicalize_quote_v1(value: str) -> str:
    """按 §11.1 规范化引用，供连续子串校验与脱敏 offset 计算。"""
    text = unicodedata.normalize("NFC", value)
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"[\n\t ]+", " ", text)


def build_search_text(
    *,
    topic_group_title: str,
    topic_title: str,
    content: KnowledgeSummaryContent,
) -> str:
    """按 §7.1 固定顺序重建搜索文本，避免数据库字段漂移。"""
    parts = [topic_group_title, topic_title]
    if content.overview is not None:
        parts.append(content.overview.text)
    for section in KNOWLEDGE_SECTIONS:
        parts.extend(item.text for item in getattr(content, section))
    search_text = "\n".join(parts)
    if len(search_text) > 30_000:
        raise ValueError("知识总结搜索文本超过 30000 字符")
    return search_text


def content_hash_v1(content: KnowledgeSummaryContent) -> str:
    """按 §8.4 指定字段和数组顺序计算 content SHA-256。"""
    payload = _content_payload(content)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def state_hash_v1(
    *,
    topic_group_title: str,
    topic_title: str,
    content_hash: str,
    protected_sections: Iterable[str],
    review_state: str,
) -> str:
    """按 §8.4 计算标题、内容和保护状态共同决定的状态哈希。"""
    payload = {
        "normalizer_version": KNOWLEDGE_CANONICAL_VERSION,
        "topic_group_title": topic_group_title,
        "topic_title": topic_title,
        "content_hash": content_hash,
        "protected_sections": sorted(set(protected_sections)),
        "review_state": review_state,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


def excerpt_text(value: str | None, *, max_length: int) -> str | None:
    """保留固定长度的页面摘要，不修改原始内容。"""
    if value is None:
        return None
    return value[:max_length]


def _content_payload(content: KnowledgeSummaryContent) -> dict[str, Any]:
    """将 Pydantic content 转为 §8.4 所需的固定字段顺序 JSON 对象。"""
    return {
        "schema_version": KNOWLEDGE_CONTENT_SCHEMA_VERSION,
        "overview": _item_payload(content.overview),
        "definitions": [_item_payload(item) for item in content.definitions],
        "theorems": [_item_payload(item) for item in content.theorems],
        "formulas": [_item_payload(item) for item in content.formulas],
        "properties": [_item_payload(item) for item in content.properties],
        "methods": [_item_payload(item) for item in content.methods],
        "pitfalls": [_item_payload(item) for item in content.pitfalls],
    }


def _item_payload(item: Any) -> dict[str, Any] | None:
    """序列化条目 UUID 与枚举，保持数据库 JSON 中的稳定小写表示。"""
    if item is None:
        return None
    return {
        "item_id": _uuid_text(item.item_id),
        "text": item.text,
        "origin": item.origin,
        "source_ids": [_uuid_text(source_id) for source_id in item.source_ids],
    }


def _uuid_text(value: UUID) -> str:
    """UUID 的小写文本表示是 content/state hash 的一部分。"""
    return str(value).lower()
