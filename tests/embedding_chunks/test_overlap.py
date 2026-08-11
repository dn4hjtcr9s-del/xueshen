"""验证 overlap 只复制同一语义单元内的可拆正文 token。"""

from scripts.embedding_chunks.chunker import chunk_semantic_unit
from scripts.embedding_chunks.schemas import SemanticSegment, SemanticUnit, SourceRef
from scripts.embedding_chunks.tokenizer import WhitespaceTokenizer


def _unit(text: str, sequence: int) -> SemanticUnit:
    ref = SourceRef(
        source_page=sequence + 1,
        mineru_page_index=sequence,
        block_index=sequence,
        source_chunk_id="chunk",
        source_pdf="book.pdf",
        element_type="text",
        bbox=(0, 0, 1, 1),
        raw_hash=f"{sequence:064x}",
    )
    return SemanticUnit(
        book_id="01_book",
        book_name="教材",
        grade_level="大学",
        section="body",
        chapter_path=(f"第{sequence + 1}章",),
        content_role="body",
        retrieval_weight=1.0,
        segments=(SemanticSegment(text, (ref,)),),
        sequence=sequence,
    )


def test_overlap_repeats_only_previous_body_tail() -> None:
    tokenizer = WhitespaceTokenizer()
    unit = _unit("t0 t1 t2 t3 t4 t5 t6 t7 t8", 0)

    chunks, _ = chunk_semantic_unit(unit, tokenizer, chunk_size=8, overlap=2)

    assert [chunk.content_text for chunk in chunks] == [
        "t0 t1 t2 t3 t4",
        "t3 t4 t5 t6 t7",
        "t6 t7 t8",
    ]


def test_overlap_does_not_cross_semantic_unit_boundary() -> None:
    tokenizer = WhitespaceTokenizer()
    first, _ = chunk_semantic_unit(
        _unit("a0 a1 a2 a3 a4 a5", 0), tokenizer, chunk_size=8, overlap=2
    )
    second, _ = chunk_semantic_unit(
        _unit("b0 b1 b2 b3 b4 b5", 1), tokenizer, chunk_size=8, overlap=2
    )

    assert first[-1].content_text.startswith("a")
    assert second[0].content_text == "b0 b1 b2 b3 b4"
    assert "a" not in second[0].content_text
