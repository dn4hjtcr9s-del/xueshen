"""公式检索测试：支持带 LaTeX 分隔符和直接公式输入。"""

from __future__ import annotations

from backend.rag.retrieval import prepare_formula_query


def test_prepare_formula_query_extracts_latex_formula() -> None:
    assert prepare_formula_query("求解 $x^2 + 2x + 1 = 0$") == ("x^2+2x+1=0",)


def test_prepare_formula_query_accepts_direct_formula() -> None:
    assert prepare_formula_query(" f(x) = x^2 ") == ("f(x)=x^2",)
