"""验证 clean JSONL 的严格解析和必需溯源字段。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.embedding_chunks.source_reader import SourceReadError, read_clean_records


def _record() -> dict[str, object]:
    return {
        "book_id": "01_book",
        "book_name": "教材",
        "grade_level": "大学",
        "source_page": 7,
        "source_page_end": 7,
        "section": "body",
        "element_type": "text",
        "text": "函数的定义。",
        "extra": {},
        "source_refs": [
            {
                "source_page": 7,
                "mineru_page_index": 6,
                "block_index": 3,
                "source_chunk_id": "0001_pages",
                "source_pdf": "book.pdf",
                "element_type": "text",
                "bbox": [1, 2, 3, 4],
                "raw_hash": "a" * 64,
            }
        ],
    }


def test_read_clean_records_returns_typed_records(tmp_path: Path) -> None:
    path = tmp_path / "clean_content_list.jsonl"
    path.write_text(json.dumps(_record(), ensure_ascii=False) + "\n", encoding="utf-8")

    records = list(read_clean_records(path))

    assert len(records) == 1
    assert records[0].book_id == "01_book"
    assert records[0].source_page_end == 7
    assert records[0].source_refs[0].key == (7, 3)
    assert records[0].source_refs[0].bbox == (1, 2, 3, 4)


def test_read_clean_records_rejects_missing_source_refs(tmp_path: Path) -> None:
    payload = _record()
    payload.pop("source_refs")
    path = tmp_path / "clean_content_list.jsonl"
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    with pytest.raises(SourceReadError, match="source_refs"):
        list(read_clean_records(path))
