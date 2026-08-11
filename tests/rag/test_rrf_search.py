"""混合检索测试：RRF 保留引用并降低 answer_key 排名。"""

from __future__ import annotations

from backend.rag.retrieval import fuse_search_hits
from backend.rag.schemas import SearchHit


def _hit(chunk_id: str, *, role: str, weight: float) -> SearchHit:
    return SearchHit(
        chunk_id=chunk_id,
        score=1.0,
        retrieval_weight=weight,
        book_id="book-1",
        book_name="测试教材",
        grade_level="高中",
        section="正文",
        chapter_path=("第一章",),
        content_role=role,
        content_text="正文",
        source_page_start=3,
        source_page_end=4,
        source_refs=({"source_page": 3},),
    )


def test_fuse_search_hits_preserves_provenance_and_applies_weight() -> None:
    body = _hit("body", role="body", weight=1.0)
    answer = _hit("answer", role="answer_key", weight=0.65)

    fused = fuse_search_hits(
        {"vector": [answer, body], "fts": [body, answer]},
        rrf_k=60,
        limit=10,
    )

    assert [hit.chunk_id for hit in fused] == ["body", "answer"]
    assert fused[0].source_page_start == 3
    assert fused[0].matched_channels == ("fts", "vector")
