"""检索契约：SearchFilters 合法词表、检索 Worker 输入输出与证据集（方案 §11/§12/§13）。

- semantic_filters 只允许 SearchFilters 五个字段（§11.1 / §4 Q9）；
- RetrievalWorkerResult 是 LangGraph Send × N 动态 Worker 的独立结果；
- evidence_set 是最终证据（含合并后的相邻 chunk 与 Citation）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from backend.conversation.contracts.api import Citation

#: semantic_filters 允许的字段（§11.1：book_ids/grade_levels/sections/content_roles/chapter_prefix）
SEARCH_FILTER_FIELDS = frozenset(
    {"book_ids", "grade_levels", "sections", "content_roles", "chapter_prefix"}
)

FILTER_VOCABULARY_VERSION = "1"


@dataclass(frozen=True)
class ActiveCorpusVocabulary:
    """active corpus 合法过滤词表（§4 Q9 / §11.1），随 RewriteContextView 注入 prompt。"""

    version: str = FILTER_VOCABULARY_VERSION
    allowed_book_ids: tuple[str, ...] = ()
    allowed_grade_levels: tuple[str, ...] = ()
    allowed_sections: tuple[str, ...] = ()
    allowed_content_roles: tuple[str, ...] = ()
    allowed_chapter_prefixes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetrievalWorkerInput:
    plan_revision: int
    subquery_id: str
    query_text: str
    query_vector: tuple[float, ...] | None
    validated_filters: dict[str, list[str]] = field(default_factory=dict)
    limit: int = 20
    deadline: datetime | None = None


@dataclass(frozen=True)
class SearchHitRef:
    """SearchHit 的不可变引用（不携带向量或敏感字段）。"""

    chunk_id: str
    corpus_id: str
    chunk_index: int
    token_count: int | None
    score: float
    book_id: str
    book_name: str
    grade_level: str
    section: str
    chapter_path: tuple[str, ...]
    content_role: str
    content_text: str
    source_page_start: int | None
    source_page_end: int | None
    source_refs: tuple[dict[str, object], ...]


@dataclass(frozen=True)
class RetrievalWorkerResult:
    worker_key: str
    subquery_id: str
    normalized_query: str
    status: Literal["succeeded", "timed_out", "failed", "cancelled"]
    hits: tuple[SearchHitRef, ...] = ()
    latency_ms: float = 0.0
    attempt_count: int = 1
    error_code: str | None = None


@dataclass(frozen=True)
class MergedEvidence:
    """聚合、去重、相邻合并后的单条证据（§13.1）。"""

    evidence_id: str
    chunk_ids: tuple[str, ...]
    corpus_id: str
    book_id: str
    book_name: str
    chapter_path: tuple[str, ...]
    content_role: str
    content_text: str
    token_count: int
    score: float
    matched_subquery_ids: tuple[str, ...]
    source_refs: tuple[dict[str, object], ...]
    citation: Citation


@dataclass(frozen=True)
class EvidenceSet:
    """最终证据集（§13/§14），进入 Answer 节点与 Citation allow-list。"""

    items: tuple[MergedEvidence, ...] = ()
    total_tokens: int = 0

    def ids(self) -> frozenset[str]:
        return frozenset(item.evidence_id for item in self.items)


@dataclass(frozen=True)
class EvidenceSetResult:
    evidence_set: EvidenceSet
    deduplicated: int = 0
    merged: int = 0
    truncated_tokens: int = 0
