"""聚合检查最终 embedding chunks，并以稳定错误码阻止污染产物发布。"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

from scripts.embedding_chunks.identifiers import content_hash, source_hash
from scripts.embedding_chunks.schemas import ChunkRecord
from scripts.embedding_chunks.tokenizer import Tokenizer

_HTML_TAG_PATTERN = re.compile(r"<\s*/?\s*[a-zA-Z][^>]*>")
_IMAGE_PATH_PATTERN = re.compile(
    r"(?:^|[\s\"'(<>=])[^\s\"'<>]*\.(?:png|jpe?g|gif|webp|svg|bmp|tiff?)(?=$|[\s\"')<>])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class QualityIssue:
    """单个 chunk 的质量问题。"""

    code: str
    chunk_id: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "chunk_id": self.chunk_id, "message": self.message}


@dataclass(frozen=True, slots=True)
class QualityReport:
    """整批 chunks 的质量门禁结果。"""

    total_chunks: int
    max_token_count: int
    error_counts: dict[str, int]
    issues: tuple[QualityIssue, ...]

    @property
    def passed(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "total_chunks": self.total_chunks,
            "max_token_count": self.max_token_count,
            "error_counts": self.error_counts,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def validate_chunks(
    chunks: list[ChunkRecord], tokenizer: Tokenizer, chunk_size: int
) -> QualityReport:
    """检查重复 ID、内容污染、token、哈希及页码范围的一致性。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")

    issues: list[QualityIssue] = []
    seen_ids: set[str] = set()
    max_token_count = 0

    def add(code: str, chunk: ChunkRecord, message: str) -> None:
        issues.append(QualityIssue(code=code, chunk_id=chunk.chunk_id, message=message))

    for chunk in chunks:
        if chunk.chunk_id in seen_ids:
            add("duplicate_chunk_id", chunk, "chunk_id 在当前数据集中重复")
        seen_ids.add(chunk.chunk_id)

        combined_text = f"{chunk.content_text}\n{chunk.embedding_text}"
        if _HTML_TAG_PATTERN.search(combined_text):
            add("html_contamination", chunk, "正文或 embedding_text 含 HTML 标签")
        if _IMAGE_PATH_PATTERN.search(combined_text):
            add("image_path_contamination", chunk, "正文或 embedding_text 含图片文件路径")

        actual_tokens = tokenizer.count(chunk.embedding_text)
        max_token_count = max(max_token_count, actual_tokens)
        if chunk.token_count != actual_tokens:
            add(
                "token_count_mismatch",
                chunk,
                f"记录 token_count={chunk.token_count}，实际为 {actual_tokens}",
            )
        if actual_tokens > chunk_size:
            add(
                "token_limit_exceeded",
                chunk,
                f"实际 token 数 {actual_tokens} 超过上限 {chunk_size}",
            )

        if chunk.source_page_start > chunk.source_page_end:
            add("invalid_page_range", chunk, "source_page_start 大于 source_page_end")
        if chunk.source_refs:
            expected_start = min(ref.source_page for ref in chunk.source_refs)
            expected_end = max(ref.source_page for ref in chunk.source_refs)
            if (chunk.source_page_start, chunk.source_page_end) != (
                expected_start,
                expected_end,
            ):
                add(
                    "source_page_range_mismatch",
                    chunk,
                    "记录页码范围与 source_refs 不一致",
                )
        else:
            add("missing_source_refs", chunk, "chunk 缺少 source_refs")

        if chunk.content_hash != content_hash(chunk.content_text):
            add("content_hash_mismatch", chunk, "content_hash 与正文不一致")
        if chunk.source_hash != source_hash(chunk.source_refs):
            add("source_hash_mismatch", chunk, "source_hash 与 source_refs 不一致")
        if chunk.tokenizer_id != tokenizer.tokenizer_id:
            add("tokenizer_id_mismatch", chunk, "tokenizer_id 与质量检查 tokenizer 不一致")

    error_counts = dict(sorted(Counter(issue.code for issue in issues).items()))
    return QualityReport(
        total_chunks=len(chunks),
        max_token_count=max_token_count,
        error_counts=error_counts,
        issues=tuple(issues),
    )
