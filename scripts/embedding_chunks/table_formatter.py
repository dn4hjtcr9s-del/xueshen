"""把 MinerU HTML 表格转换为无 HTML 标签的行列文本。"""

from __future__ import annotations

from html.parser import HTMLParser


class _TableParser(HTMLParser):
    """只提取 tr/th/td；复杂 span 以文字标记降级而不猜测网格。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell_parts: list[str] | None = None
        self._cell_suffix = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in {"td", "th"}:
            self._cell_parts = []
            values = dict(attrs)
            suffixes: list[str] = []
            if values.get("rowspan") not in {None, "", "1"}:
                suffixes.append(f"跨{values['rowspan']}行")
            if values.get("colspan") not in {None, "", "1"}:
                suffixes.append(f"跨{values['colspan']}列")
            self._cell_suffix = f"（{'，'.join(suffixes)}）" if suffixes else ""
        elif tag == "br" and self._cell_parts is not None:
            self._cell_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell_parts is not None:
            self._cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell_parts is not None:
            text = " ".join("".join(self._cell_parts).split()) + self._cell_suffix
            if self._row is None:
                self._row = []
            self._row.append(text)
            self._cell_parts = None
            self._cell_suffix = ""
        elif tag == "tr" and self._row is not None:
            if any(cell for cell in self._row):
                self.rows.append(self._row)
            self._row = None


def linearize_table(html: str, caption: str) -> str:
    """将 HTML 表格转换为“表题 + 第N行”的稳定纯文本。"""
    parser = _TableParser()
    parser.feed(html)
    parser.close()
    lines: list[str] = []
    normalized_caption = " ".join(caption.split())
    if normalized_caption:
        lines.append(f"表题：{normalized_caption}")
    lines.extend(
        f"第{row_number}行：{' | '.join(cell or '空' for cell in row)}"
        for row_number, row in enumerate(parser.rows, start=1)
    )
    return "\n".join(lines)
