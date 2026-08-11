"""RAG 检索公开数据结构：过滤条件和带可回溯引用的命中结果。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """所有召回通道共享的结构化过滤条件。"""

    book_ids: tuple[str, ...] = ()
    grade_levels: tuple[str, ...] = ()
    sections: tuple[str, ...] = ()
    content_roles: tuple[str, ...] = ()
    chapter_prefix: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchHit:
    """检索结果；页码和 source_refs 可直接用于回答引用。"""

    chunk_id: str
    score: float
    retrieval_weight: float
    book_id: str
    book_name: str
    grade_level: str
    section: str
    chapter_path: tuple[str, ...]
    content_role: str
    content_text: str
    source_page_start: int
    source_page_end: int
    source_refs: tuple[dict[str, Any], ...]
    matched_channels: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> SearchHit:
        """把 SQLAlchemy mapping 转换为稳定的业务对象。"""
        refs = row.get("source_refs") or []
        path = row.get("chapter_path") or []
        return cls(
            chunk_id=str(row["chunk_id"]),
            score=float(row["score"]),
            retrieval_weight=float(row["retrieval_weight"]),
            book_id=str(row["book_id"]),
            book_name=str(row["book_name"]),
            grade_level=str(row["grade_level"]),
            section=str(row["section"]),
            chapter_path=tuple(str(item) for item in path),
            content_role=str(row["content_role"]),
            content_text=str(row["content_text"]),
            source_page_start=int(row["source_page_start"]),
            source_page_end=int(row["source_page_end"]),
            source_refs=tuple(dict(item) for item in refs),
            matched_channels=tuple(str(item) for item in row.get("matched_channels", ())),
        )
