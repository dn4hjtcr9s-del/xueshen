"""验证教材记录的内容角色和排除原因分类。"""

from __future__ import annotations

import pytest

from scripts.embedding_chunks.role_classifier import classify_record
from scripts.embedding_chunks.schemas import CleanRecord, SourceRef


@pytest.fixture()
def source_ref() -> SourceRef:
    return SourceRef(1, 0, 0, "chunk", "book.pdf", "text", (0, 0, 1, 1), "a" * 64)


def _record(
    source_ref: SourceRef,
    text: str,
    *,
    section: str = "body",
    element_type: str = "text",
    extra: dict[str, object] | None = None,
) -> CleanRecord:
    return CleanRecord(
        book_id="01_book",
        book_name="教材",
        grade_level="大学",
        source_page=1,
        source_page_end=1,
        section=section,
        element_type=element_type,
        text=text,
        extra=extra or {},
        source_refs=(source_ref,),
    )


@pytest.mark.parametrize(
    ("text", "expected_role"),
    [
        ("定义 设集合A非空。", "definition"),
        ("定理1 若函数连续，则……", "theorem"),
        ("引理 下面结论成立。", "theorem"),
        ("证明 由定义可知。", "proof"),
        ("例3 求下列极限。", "example"),
        ("解：使用洛必达法则。", "solution"),
        ("习题1.2", "exercise"),
        ("练习 求函数定义域。", "exercise"),
    ],
)
def test_classifies_math_roles(
    source_ref: SourceRef, text: str, expected_role: str
) -> None:
    result = classify_record(_record(source_ref, text), ("第一章",))

    assert result.role == expected_role
    assert result.exclusion_reason is None


def test_answer_key_overrides_normal_text_role(source_ref: SourceRef) -> None:
    result = classify_record(
        _record(source_ref, "1. A", section="back_matter"),
        ("部分习题答案",),
    )

    assert result.role == "answer_key"
    assert result.retrieval_weight == 0.65


@pytest.mark.parametrize("heading", ["参考文献", "索引", "中英文名词索引"])
def test_reference_and_index_are_excluded(source_ref: SourceRef, heading: str) -> None:
    result = classify_record(_record(source_ref, "条目"), (heading,))

    assert result.role is None
    assert result.exclusion_reason == "non_content_back_matter"


def test_appendix_is_retained(source_ref: SourceRef) -> None:
    result = classify_record(_record(source_ref, "常用积分公式"), ("附录A",))

    assert result.role == "appendix"


def test_front_matter_is_excluded(source_ref: SourceRef) -> None:
    result = classify_record(_record(source_ref, "出版社", section="front_matter"), ())

    assert result.exclusion_reason == "front_matter"


def test_uncaptioned_image_is_excluded_but_caption_is_retained(source_ref: SourceRef) -> None:
    no_caption = classify_record(
        _record(source_ref, "images/a.jpg", element_type="image"),
        ("第一章",),
    )
    captioned = classify_record(
        _record(
            source_ref,
            "images/a.jpg",
            element_type="image",
            extra={"caption": "图1.1 函数图像"},
        ),
        ("第一章",),
    )

    assert no_caption.exclusion_reason == "image_without_caption"
    assert captioned.role == "figure_caption"
