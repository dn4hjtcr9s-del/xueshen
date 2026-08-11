"""Embedding chunk 流水线共享的数据模型和 JSON 序列化边界。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ContentRole = Literal[
    "body",
    "definition",
    "theorem",
    "proof",
    "example",
    "solution",
    "exercise",
    "answer_key",
    "formula",
    "table",
    "figure_caption",
    "appendix",
]


@dataclass(frozen=True, slots=True)
class SourceRef:
    """指向 MinerU 原始 content_list 中唯一 block 的精确引用。"""

    source_page: int
    mineru_page_index: int
    block_index: int
    source_chunk_id: str
    source_pdf: str
    element_type: str
    bbox: tuple[int | float, ...]
    raw_hash: str

    @property
    def key(self) -> tuple[int, int]:
        return self.source_page, self.block_index

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SourceRef:
        bbox_value = payload.get("bbox", [])
        if not isinstance(bbox_value, list):
            raise ValueError("bbox 必须是数组")
        return cls(
            source_page=int(payload["source_page"]),
            mineru_page_index=int(payload["mineru_page_index"]),
            block_index=int(payload["block_index"]),
            source_chunk_id=str(payload.get("source_chunk_id", "")),
            source_pdf=str(payload.get("source_pdf", "")),
            element_type=str(payload.get("element_type", "")),
            bbox=tuple(
                float(value) if isinstance(value, float) else int(value)
                for value in bbox_value
            ),
            raw_hash=str(payload["raw_hash"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_page": self.source_page,
            "mineru_page_index": self.mineru_page_index,
            "block_index": self.block_index,
            "source_chunk_id": self.source_chunk_id,
            "source_pdf": self.source_pdf,
            "element_type": self.element_type,
            "bbox": list(self.bbox),
            "raw_hash": self.raw_hash,
        }


@dataclass(frozen=True, slots=True)
class CleanRecord:
    """由 clean_content_list.jsonl 解析出的单条清洗记录。"""

    book_id: str
    book_name: str
    grade_level: str
    source_page: int
    source_page_end: int
    section: str
    element_type: str
    text: str
    extra: dict[str, Any]
    source_refs: tuple[SourceRef, ...]
    level: int | None = None
    sequence: int = 0


@dataclass(frozen=True, slots=True)
class SemanticSegment:
    """语义单元内可独立追踪的文本段；公式和表格通常不可拆分。"""

    text: str
    source_refs: tuple[SourceRef, ...]
    splittable: bool = True
    element_type: str = "text"


@dataclass(frozen=True, slots=True)
class SemanticUnit:
    """在章节和角色边界内组合后的最小语义单元。"""

    book_id: str
    book_name: str
    grade_level: str
    section: str
    chapter_path: tuple[str, ...]
    content_role: ContentRole
    retrieval_weight: float
    segments: tuple[SemanticSegment, ...]
    sequence: int

    @property
    def content_text(self) -> str:
        return "\n\n".join(
            segment.text.strip() for segment in self.segments if segment.text.strip()
        )

    @property
    def source_refs(self) -> tuple[SourceRef, ...]:
        seen: set[tuple[int, int]] = set()
        refs: list[SourceRef] = []
        for segment in self.segments:
            for ref in segment.source_refs:
                if ref.key not in seen:
                    refs.append(ref)
                    seen.add(ref.key)
        return tuple(refs)


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """最终写入 chunks.jsonl 的单条 embedding 记录。"""

    schema_version: str
    chunk_id: str
    chunk_index: int
    book_id: str
    book_name: str
    grade_level: str
    section: str
    content_text: str
    embedding_text: str
    chapter_path: tuple[str, ...]
    content_role: ContentRole
    retrieval_weight: float
    source_page_start: int
    source_page_end: int
    source_refs: tuple[SourceRef, ...]
    token_count: int
    tokenizer_id: str
    content_hash: str
    source_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "book_id": self.book_id,
            "book_name": self.book_name,
            "grade_level": self.grade_level,
            "section": self.section,
            "content_text": self.content_text,
            "embedding_text": self.embedding_text,
            "chapter_path": list(self.chapter_path),
            "content_role": self.content_role,
            "retrieval_weight": self.retrieval_weight,
            "source_page_start": self.source_page_start,
            "source_page_end": self.source_page_end,
            "source_refs": [ref.to_dict() for ref in self.source_refs],
            "token_count": self.token_count,
            "tokenizer_id": self.tokenizer_id,
            "content_hash": self.content_hash,
            "source_hash": self.source_hash,
        }


@dataclass(frozen=True, slots=True)
class ExcludedRecord:
    """记录被排除内容及其原因，保证所有丢弃行为可审计。"""

    book_id: str
    reason: str
    text: str
    source_refs: tuple[SourceRef, ...] = field(default_factory=tuple)
    element_type: str = ""
    chapter_path: tuple[str, ...] = field(default_factory=tuple)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "book_id": self.book_id,
            "reason": self.reason,
            "text": self.text,
            "element_type": self.element_type,
            "chapter_path": list(self.chapter_path),
            "source_refs": [ref.to_dict() for ref in self.source_refs],
            "details": self.details,
        }
