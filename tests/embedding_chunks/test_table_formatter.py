"""验证 HTML 表格被转换为无标签、可检索的线性文本。"""

from scripts.embedding_chunks.table_formatter import linearize_table


def test_linearize_table_preserves_caption_rows_and_entities() -> None:
    html = (
        "<table><tr><th>x</th><th>f(x)</th></tr>"
        "<tr><td>-1</td><td>x &lt; 0</td></tr></table>"
    )

    text = linearize_table(html, "表1 函数值")

    assert text == "表题：表1 函数值\n第1行：x | f(x)\n第2行：-1 | x < 0"
    assert "<td" not in text


def test_linearize_table_marks_spans_without_repeating_cells() -> None:
    html = "<table><tr><td rowspan='2'>A</td><td colspan='2'>B</td></tr></table>"

    text = linearize_table(html, "")

    assert text == "第1行：A（跨2行） | B（跨2列）"
