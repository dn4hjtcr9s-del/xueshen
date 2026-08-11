"""验证 OCR 清洗过程完整保留原始 block 的精确溯源信息。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.clean_ocr import process_book


def _raw_record(
    *,
    page: int,
    block_index: int,
    element_type: str,
    content: dict[str, Any],
) -> dict[str, Any]:
    raw = {"type": element_type, "content": content, "bbox": [10, 20, 300, 80]}
    return {
        "book_id": "99_test_book",
        "book_name": "测试教材",
        "source_pdf": "test.pdf",
        "source_page": page,
        "chunk_id": "0001_pages_0001_0003",
        "mineru_page_index": page - 1,
        "block_index": block_index,
        "element_type": element_type,
        "include_in_embedding": True,
        "text": "",
        "raw": raw,
    }


def _raw_hash(record: dict[str, Any]) -> str:
    payload = json.dumps(record["raw"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_process_book_preserves_refs_across_paragraph_and_formula_merges(tmp_path: Path) -> None:
    book_dir = tmp_path / "ocr" / "99_test_book"
    outroot = tmp_path / "clean"
    book_dir.mkdir(parents=True)
    (book_dir / "book.json").write_text(
        json.dumps({"book_id": "99_test_book", "book_name": "测试教材"}),
        encoding="utf-8",
    )

    records = [
        _raw_record(
            page=1,
            block_index=0,
            element_type="title",
            content={"title_content": [{"type": "text", "content": "第一章 测试"}], "level": 1},
        ),
        _raw_record(
            page=1,
            block_index=1,
            element_type="text",
            content={
                "paragraph_content": [
                    {
                        "type": "text",
                        "content": (
                            "这是一个长度足够且没有终止标点的测试段落，"
                            "用来验证跨页正文合并时的精确溯源"
                        ),
                    }
                ]
            },
        ),
        _raw_record(
            page=2,
            block_index=0,
            element_type="text",
            content={"paragraph_content": [{"type": "text", "content": "继续到下一页。"}]},
        ),
        _raw_record(
            page=2,
            block_index=1,
            element_type="equation_interline",
            content={"math_content": r"A=\{x", "image_source": {"path": "formula-a.png"}},
        ),
        _raw_record(
            page=3,
            block_index=0,
            element_type="equation_interline",
            content={"math_content": r"\}\subset B", "image_source": {"path": "formula-b.png"}},
        ),
    ]
    (book_dir / "content_list.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )

    process_book(book_dir, outroot)

    clean_records = [
        json.loads(line)
        for line in (outroot / "99_test_book" / "clean_content_list.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    paragraph = next(record for record in clean_records if record["element_type"] == "text")
    formula = next(
        record for record in clean_records if record["element_type"] == "equation_interline"
    )

    assert paragraph["source_page"] == 1
    assert paragraph["source_page_end"] == 2
    assert [(ref["source_page"], ref["block_index"]) for ref in paragraph["source_refs"]] == [
        (1, 1),
        (2, 0),
    ]
    assert paragraph["source_refs"][0]["source_chunk_id"] == "0001_pages_0001_0003"
    assert paragraph["source_refs"][0]["source_pdf"] == "test.pdf"
    assert paragraph["source_refs"][0]["bbox"] == [10, 20, 300, 80]
    assert paragraph["source_refs"][0]["raw_hash"] == _raw_hash(records[1])

    assert formula["source_page"] == 2
    assert formula["source_page_end"] == 3
    assert [(ref["source_page"], ref["block_index"]) for ref in formula["source_refs"]] == [
        (2, 1),
        (3, 0),
    ]
    assert formula["source_refs"][1]["raw_hash"] == _raw_hash(records[4])
