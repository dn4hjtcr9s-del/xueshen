"""验证目录空格、正文附录和真正后置材料的 section 切换。"""

from __future__ import annotations

from typing import Any

from scripts.clean_ocr import assign_sections, extract_chapter_header_hint


def _block(kind: str, text: str, page: int) -> dict[str, Any]:
    return {
        "kind": kind,
        "text": text,
        "source_page": page,
        "source_refs": [],
    }


def test_assign_sections_accepts_spaced_chapter_and_keeps_appendix_in_body() -> None:
    blocks = [
        _block("title", "高等代数", 1),
        _block("text", "目录与前言", 5),
        _block("title", "第 7 章 一元和 n 元多项式环", 30),
        _block("text", "正文内容", 31),
        _block("title", "附录 Dedekind 切割定理", 44),
        _block("text", "附录也是教材正文", 45),
        _block("title", "习题答案与提示", 670),
        _block("text", "第一章答案", 671),
    ]

    assigned = assign_sections(blocks)

    assert [block["section"] for block in assigned] == [
        "front_matter",
        "front_matter",
        "body",
        "body",
        "body",
        "body",
        "back_matter",
        "back_matter",
    ]


def test_assign_sections_detects_unlabelled_answer_key_by_repeated_first_chapter() -> None:
    blocks = [
        _block("title", "教材", 1),
        _block("title", "第一章 函数", 20),
        _block("text", "第一章正文", 21),
        _block("title", "第二章 极限", 80),
        _block("text", "第二章正文", 81),
        _block("title", "第一章", 346),
        _block("text", "1. 答案与提示", 347),
        _block("title", "郑重声明", 380),
    ]

    assigned = assign_sections(blocks)

    assert [block["section"] for block in assigned] == [
        "front_matter",
        "body",
        "body",
        "body",
        "body",
        "back_matter",
        "back_matter",
        "back_matter",
    ]


def test_assign_sections_marks_publication_notice_as_back_matter() -> None:
    blocks = [
        _block("title", "教材", 1),
        _block("title", "第一章 函数", 20),
        _block("text", "正文", 21),
        _block("title", "郑重声明", 100),
        _block("text", "防伪查询说明", 101),
    ]

    assigned = assign_sections(blocks)

    assert [block["section"] for block in assigned] == [
        "front_matter",
        "body",
        "body",
        "back_matter",
        "back_matter",
    ]


def test_assign_sections_uses_dropped_page_header_as_missing_chapter_hint() -> None:
    blocks = [
        _block("title", "工程数学 线性代数", 1),
        _block("title", "目录", 13),
        _block("title", "§ 1 二阶与三阶行列式", 14),
        _block("text", "第一章正文", 14),
        _block("title", "第 3 章 矩阵的初等变换", 69),
        _block("title", "第一章", 180),
        _block("text", "答案", 181),
    ]

    assigned = assign_sections(blocks, chapter_header_hints=[(14, "1")])

    assert [block["section"] for block in assigned] == [
        "front_matter",
        "front_matter",
        "body",
        "body",
        "body",
        "back_matter",
        "back_matter",
    ]


def test_extract_chapter_header_hint_reads_dropped_header_text() -> None:
    record = {
        "element_type": "page_header",
        "source_page": 14,
        "raw": {
            "content": {"page_header_content": [{"type": "text", "content": "第 1 章 行列式"}]}
        },
    }

    assert extract_chapter_header_hint(record) == (14, "1")
