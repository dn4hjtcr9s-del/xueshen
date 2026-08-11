"""验证标题继承和数学内容的语义组合规则。"""

from __future__ import annotations

from scripts.embedding_chunks.schemas import CleanRecord, SourceRef
from scripts.embedding_chunks.semantic_units import build_semantic_units


def _record(
    sequence: int,
    text: str,
    *,
    element_type: str = "text",
    level: int | None = None,
    section: str = "body",
    extra: dict[str, object] | None = None,
) -> CleanRecord:
    ref = SourceRef(
        source_page=sequence + 1,
        mineru_page_index=sequence,
        block_index=sequence,
        source_chunk_id="chunk",
        source_pdf="book.pdf",
        element_type=element_type,
        bbox=(0, 0, 1, 1),
        raw_hash=f"{sequence:064x}",
    )
    return CleanRecord(
        book_id="01_book",
        book_name="教材",
        grade_level="大学",
        source_page=sequence + 1,
        source_page_end=sequence + 1,
        section=section,
        element_type=element_type,
        text=text,
        extra=extra or {},
        source_refs=(ref,),
        level=level,
        sequence=sequence,
    )


def test_theorem_formula_and_proof_form_one_unit() -> None:
    records = [
        _record(0, "第一章 极限", element_type="title", level=1),
        _record(1, "1.1 数列极限", element_type="title", level=2),
        _record(2, "定理1 收敛数列有界。"),
        _record(3, r"a_n \\to A", element_type="equation_interline"),
        _record(4, "证明 由收敛定义可知。"),
    ]

    units, excluded = build_semantic_units(records)

    assert excluded == []
    assert len(units) == 1
    assert units[0].content_role == "theorem"
    assert units[0].chapter_path == ("第一章 极限", "1.1 数列极限")
    assert "定理1" in units[0].content_text
    assert "$$\na_n" in units[0].content_text
    assert "证明" in units[0].content_text
    assert units[0].segments[1].splittable is False


def test_example_and_solution_bind_but_new_heading_flushes() -> None:
    records = [
        _record(0, "第一章", element_type="title", level=1),
        _record(1, "例1 求极限。"),
        _record(2, "解：结果为0。"),
        _record(3, "第二节 导数", element_type="title", level=2),
        _record(4, "定义 导数是差商的极限。"),
    ]

    units, _ = build_semantic_units(records)

    assert [unit.content_role for unit in units] == ["example", "definition"]
    assert "解：" in units[0].content_text
    assert units[1].chapter_path == ("第一章", "第二节 导数")


def test_table_and_caption_are_linearized_without_image_paths() -> None:
    records = [
        _record(0, "第一章", element_type="title", level=1),
        _record(1, "观察下表。"),
        _record(
            2,
            "<table><tr><td>x</td><td>1</td></tr></table>",
            element_type="table",
            extra={"caption": "表1"},
        ),
        _record(
            3,
            "images/figure.jpg",
            element_type="image",
            extra={"caption": "图1 函数图像"},
        ),
    ]

    units, excluded = build_semantic_units(records)

    assert excluded == []
    assert len(units) == 1
    assert "第1行：x | 1" in units[0].content_text
    assert "图注：图1 函数图像" in units[0].content_text
    assert "images/" not in units[0].content_text


def test_excluded_records_keep_reason_and_source_refs() -> None:
    records = [
        _record(0, "版权信息", section="front_matter"),
        _record(1, "images/a.jpg", element_type="image"),
    ]

    units, excluded = build_semantic_units(records)

    assert units == []
    assert [item.reason for item in excluded] == ["front_matter", "image_without_caption"]
    assert excluded[1].source_refs[0].block_index == 1


def test_back_matter_mode_persists_across_chapter_and_letter_headings() -> None:
    records = [
        _record(
            0,
            "习题答案与提示",
            element_type="title",
            level=1,
            section="back_matter",
        ),
        _record(1, "第一章", element_type="title", level=1, section="back_matter"),
        _record(2, "1. A；2. B。", section="back_matter"),
        _record(3, "参考文献", element_type="title", level=1, section="back_matter"),
        _record(4, "A", element_type="title", level=1, section="back_matter"),
        _record(5, "作者，书名，出版社。", section="back_matter"),
    ]

    units, excluded = build_semantic_units(records)

    assert len(units) == 1
    assert units[0].content_role == "answer_key"
    assert units[0].retrieval_weight == 0.65
    assert units[0].content_text == "1. A；2. B。"
    assert [item.reason for item in excluded] == ["non_content_back_matter"]
    assert excluded[0].text == "作者，书名，出版社。"


def test_unlabelled_back_matter_defaults_to_answer_key_and_formats_math() -> None:
    records = [
        _record(0, "第一章", element_type="title", level=1, section="back_matter"),
        _record(
            1,
            r"x \in [0, 1]",
            element_type="equation_interline",
            section="back_matter",
        ),
    ]

    units, excluded = build_semantic_units(records)

    assert excluded == []
    assert len(units) == 1
    assert units[0].content_role == "answer_key"
    assert units[0].retrieval_weight == 0.65
    assert units[0].content_text == r"""$$
x \in [0, 1]
$$"""
    assert units[0].segments[0].splittable is False
