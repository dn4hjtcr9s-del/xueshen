"""验证 embedding chunk CLI 的单书构建和失败退出码。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.build_embedding_chunks import main


def _write_single_book(clean_root: Path, raw_root: Path) -> None:
    raw_payload = {"type": "text", "text": "函数定义"}
    raw_record: dict[str, Any] = {
        "source_page": 1,
        "mineru_page_index": 0,
        "block_index": 0,
        "chunk_id": "0001_pages",
        "source_pdf": "book.pdf",
        "element_type": "text",
        "raw": raw_payload,
    }
    raw_hash = hashlib.sha256(
        json.dumps(raw_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    clean_record = {
        "book_id": "01_book",
        "book_name": "教材",
        "grade_level": "大学",
        "source_page": 1,
        "source_page_end": 1,
        "section": "body",
        "element_type": "text",
        "text": "函数定义",
        "extra": {},
        "source_refs": [
            {
                "source_page": 1,
                "mineru_page_index": 0,
                "block_index": 0,
                "source_chunk_id": "0001_pages",
                "source_pdf": "book.pdf",
                "element_type": "text",
                "bbox": [0, 0, 1, 1],
                "raw_hash": raw_hash,
            }
        ],
    }
    (clean_root / "01_book").mkdir(parents=True)
    (raw_root / "01_book").mkdir(parents=True)
    (clean_root / "01_book" / "clean_content_list.jsonl").write_text(
        json.dumps(clean_record, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (raw_root / "01_book" / "content_list.jsonl").write_text(
        json.dumps(raw_record, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def test_cli_builds_one_book_with_production_tokenizer(tmp_path: Path) -> None:
    clean_root = tmp_path / "clean"
    raw_root = tmp_path / "raw"
    output_root = tmp_path / "v1"
    _write_single_book(clean_root, raw_root)

    exit_code = main(
        [
            "--book",
            "01_book",
            "--clean-root",
            str(clean_root),
            "--raw-root",
            str(raw_root),
            "--output-root",
            str(output_root),
        ]
    )

    assert exit_code == 0
    manifest = json.loads((output_root / "manifest.json").read_text())
    assert manifest["book_ids"] == ["01_book"]
    assert manifest["parameters"]["chunk_size"] == 800
    assert manifest["parameters"]["chunk_overlap"] == 100
    assert manifest["parameters"]["tokenizer_id"] == "tiktoken:cl100k_base"


def test_cli_returns_nonzero_for_missing_input(tmp_path: Path) -> None:
    exit_code = main(
        [
            "--all",
            "--clean-root",
            str(tmp_path / "missing-clean"),
            "--raw-root",
            str(tmp_path / "missing-raw"),
            "--output-root",
            str(tmp_path / "v1"),
        ]
    )

    assert exit_code == 1
