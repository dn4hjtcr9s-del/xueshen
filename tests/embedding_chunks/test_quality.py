"""验证最终 chunk 的聚合质量门禁和稳定错误码。"""

from __future__ import annotations

from dataclasses import replace

from scripts.embedding_chunks.identifiers import content_hash, source_hash, stable_chunk_id
from scripts.embedding_chunks.quality import validate_chunks
from scripts.embedding_chunks.schemas import ChunkRecord, SourceRef
from scripts.embedding_chunks.tokenizer import WhitespaceTokenizer


def _ref(page: int = 2) -> SourceRef:
    return SourceRef(
        source_page=page,
        mineru_page_index=page - 1,
        block_index=page,
        source_chunk_id="chunk",
        source_pdf="book.pdf",
        element_type="text",
        bbox=(0, 0, 1, 1),
        raw_hash=f"{page:064x}",
    )


def _chunk(
    *,
    chunk_id: str | None = None,
    content_text: str = "alpha beta",
    embedding_text: str = "book chapter alpha beta",
    refs: tuple[SourceRef, ...] = (_ref(),),
) -> ChunkRecord:
    content_digest = content_hash(content_text)
    source_digest = source_hash(refs)
    return ChunkRecord(
        schema_version="embedding-chunks/v1",
        chunk_id=chunk_id
        or stable_chunk_id(
            book_id="01_book",
            chapter_path=("第一章",),
            content_role="body",
            content_hash_value=content_digest,
            source_hash_value=source_digest,
        ),
        chunk_index=0,
        book_id="01_book",
        book_name="教材",
        grade_level="大学",
        section="body",
        content_text=content_text,
        embedding_text=embedding_text,
        chapter_path=("第一章",),
        content_role="body",
        retrieval_weight=1.0,
        source_page_start=min(ref.source_page for ref in refs),
        source_page_end=max(ref.source_page for ref in refs),
        source_refs=refs,
        token_count=len(embedding_text.split()),
        tokenizer_id="whitespace-v1",
        content_hash=content_digest,
        source_hash=source_digest,
    )


def test_validate_chunks_accepts_consistent_records() -> None:
    report = validate_chunks([_chunk()], WhitespaceTokenizer(), chunk_size=8)

    assert report.passed is True
    assert report.total_chunks == 1
    assert report.max_token_count == 4
    assert report.error_counts == {}
    assert report.to_dict()["passed"] is True


def test_validate_chunks_aggregates_duplicate_pollution_and_size_errors() -> None:
    first = _chunk(chunk_id="duplicate")
    polluted = replace(
        _chunk(
            chunk_id="duplicate",
            content_text='<table><tr><td>images/page_1.png</td></tr></table>',
            embedding_text="one two three four five six seven eight nine",
        ),
        token_count=3,
        source_page_start=9,
        source_page_end=8,
    )

    report = validate_chunks([first, polluted], WhitespaceTokenizer(), chunk_size=8)

    assert report.passed is False
    assert report.error_counts == {
        "duplicate_chunk_id": 1,
        "html_contamination": 1,
        "image_path_contamination": 1,
        "invalid_page_range": 1,
        "source_page_range_mismatch": 1,
        "token_count_mismatch": 1,
        "token_limit_exceeded": 1,
    }
    assert {issue.chunk_id for issue in report.issues} == {"duplicate"}


def test_validate_chunks_detects_hash_mismatches() -> None:
    chunk = replace(_chunk(), content_hash="0" * 64, source_hash="1" * 64)

    report = validate_chunks([chunk], WhitespaceTokenizer(), chunk_size=8)

    assert report.error_counts == {
        "content_hash_mismatch": 1,
        "source_hash_mismatch": 1,
    }
