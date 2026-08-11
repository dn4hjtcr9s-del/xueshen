"""RAG 词法预处理测试：固定中文 FTS token 和数学公式规范化。"""

from __future__ import annotations

from backend.rag.lexical import (
    LEXICAL_PIPELINE_VERSION,
    build_search_text,
    build_tsquery,
    extract_formula_terms,
)


def test_build_search_text_contains_chinese_bigrams_and_latin_terms() -> None:
    search_text = build_search_text("一元二次方程 discriminant Δ")

    tokens = set(search_text.split())
    assert {"一元", "元二", "二次", "次方", "方程"}.issubset(tokens)
    assert "discriminant" in tokens
    assert LEXICAL_PIPELINE_VERSION == "zh-bigram-formula/v1"


def test_extract_formula_terms_normalizes_equivalent_spacing() -> None:
    terms = extract_formula_terms("比较 $f(x) = x^2 + 1$ 与 \\( a + b \\)。")

    assert "f(x)=x^2+1" in terms
    assert "a+b" in terms


def test_build_tsquery_uses_safe_or_joined_lexemes() -> None:
    query = build_tsquery("一元二次方程")

    assert query == "'一元' | '元二' | '二次' | '次方' | '方程'"
