"""FTS 检索测试：空查询拒绝，中文查询生成可执行 tsquery。"""

from __future__ import annotations

import pytest

from backend.rag.retrieval import prepare_fts_query


def test_prepare_fts_query_returns_chinese_or_tsquery() -> None:
    assert prepare_fts_query("一元二次方程") == "'一元' | '元二' | '二次' | '次方' | '方程'"


def test_prepare_fts_query_rejects_empty_terms() -> None:
    with pytest.raises(ValueError, match="FTS"):
        prepare_fts_query("   ")
