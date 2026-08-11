"""向量检索边界测试：校验查询向量和结构化过滤参数。"""

from __future__ import annotations

import math

import pytest

from backend.rag.retrieval import build_filter_clause, serialize_query_vector
from backend.rag.schemas import SearchFilters


def test_serialize_query_vector_requires_finite_nonzero_dimensions() -> None:
    assert serialize_query_vector([0.1, 0.2, 0.3], dimensions=3) == "[0.1,0.2,0.3]"

    for invalid in ([0.1, 0.2], [0.0, 0.0, 0.0], [math.inf, 0.2, 0.3]):
        with pytest.raises(ValueError, match="向量"):
            serialize_query_vector(invalid, dimensions=3)


def test_build_filter_clause_supports_book_grade_role_section_and_chapter_prefix() -> None:
    clause, params = build_filter_clause(
        SearchFilters(
            book_ids=("book-1",),
            grade_levels=("高中",),
            sections=("正文",),
            content_roles=("definition", "theorem"),
            chapter_prefix=("第一章", "函数"),
        )
    )

    assert "c.book_id = ANY" in clause
    assert "c.grade_level = ANY" in clause
    assert "c.section = ANY" in clause
    assert "c.content_role = ANY" in clause
    assert "c.chapter_path[1:cardinality" in clause
    assert params["book_ids"] == ["book-1"]
    assert params["chapter_prefix"] == ["第一章", "函数"]
