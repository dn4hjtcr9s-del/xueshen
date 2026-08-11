"""验证最终 embedding 前缀计入 token 上限及原子内容安全策略。"""

from __future__ import annotations

from scripts.embedding_chunks.chunker import chunk_semantic_unit
from scripts.embedding_chunks.schemas import SemanticSegment, SemanticUnit, SourceRef
from scripts.embedding_chunks.tokenizer import WhitespaceTokenizer


def _ref(index: int, element_type: str = "text") -> SourceRef:
    return SourceRef(
        source_page=index + 1,
        mineru_page_index=index,
        block_index=index,
        source_chunk_id="chunk",
        source_pdf="book.pdf",
        element_type=element_type,
        bbox=(0, 0, 1, 1),
        raw_hash=f"{index:064x}",
    )


def _unit(*segments: SemanticSegment, role: str = "body", sequence: int = 0) -> SemanticUnit:
    return SemanticUnit(
        book_id="01_book",
        book_name="教材",
        grade_level="大学",
        section="body",
        chapter_path=("第一章",),
        content_role=role,  # type: ignore[arg-type]
        retrieval_weight=1.0,
        segments=segments,
        sequence=sequence,
    )


def test_chunk_size_counts_embedding_prefix() -> None:
    tokenizer = WhitespaceTokenizer()
    unit = _unit(SemanticSegment("a b c d e f", (_ref(0),)))

    chunks, excluded = chunk_semantic_unit(unit, tokenizer, chunk_size=8, overlap=0)

    assert excluded == []
    assert [chunk.token_count for chunk in chunks] == [8, 4]
    assert all(tokenizer.count(chunk.embedding_text) <= 8 for chunk in chunks)
    assert chunks[0].embedding_text.startswith("书名：教材\n章节：第一章\n内容类型：body\n\n")
    assert chunks[0].content_text == "a b c d e"
    assert chunks[1].content_text == "f"


def test_oversized_unsplittable_segment_is_excluded_not_truncated() -> None:
    tokenizer = WhitespaceTokenizer()
    formula = SemanticSegment(
        "a b c d e f",
        (_ref(0, "equation_interline"),),
        splittable=False,
        element_type="equation_interline",
    )

    chunks, excluded = chunk_semantic_unit(
        _unit(formula, role="formula"), tokenizer, chunk_size=8, overlap=0
    )

    assert chunks == []
    assert len(excluded) == 1
    assert excluded[0].reason == "atomic_segment_exceeds_chunk_size"
    assert excluded[0].text == "a b c d e f"
