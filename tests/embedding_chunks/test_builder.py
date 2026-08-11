"""验证双书构建、确定性产物、报告和失败时保留旧版本。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.embedding_chunks.builder import BuildConfig, BuildQualityError, build_all, build_book
from scripts.embedding_chunks.tokenizer import WhitespaceTokenizer


def _write_book(
    clean_root: Path,
    raw_root: Path,
    book_id: str,
    book_name: str,
    body_text: str,
    *,
    include_image: bool = True,
) -> None:
    raw_records: list[dict[str, Any]] = []
    clean_records: list[dict[str, Any]] = []
    specs = [
        ("title", "第一章", 1, {}),
        ("text", body_text, None, {}),
    ]
    if include_image:
        specs.append(("image", "images/page_1.png", None, {"caption": ""}))

    for index, (element_type, text, level, extra) in enumerate(specs):
        raw_payload = {"type": element_type, "text": text}
        raw_record = {
            "source_page": index + 1,
            "mineru_page_index": index,
            "block_index": index,
            "chunk_id": "0001_pages",
            "source_pdf": f"{book_id}.pdf",
            "element_type": element_type,
            "raw": raw_payload,
        }
        raw_records.append(raw_record)
        raw_hash = hashlib.sha256(
            json.dumps(
                raw_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        clean_record: dict[str, Any] = {
            "book_id": book_id,
            "book_name": book_name,
            "grade_level": "大学",
            "source_page": index + 1,
            "source_page_end": index + 1,
            "section": "body",
            "element_type": element_type,
            "text": text,
            "extra": extra,
            "source_refs": [
                {
                    "source_page": index + 1,
                    "mineru_page_index": index,
                    "block_index": index,
                    "source_chunk_id": "0001_pages",
                    "source_pdf": f"{book_id}.pdf",
                    "element_type": element_type,
                    "bbox": [0, 0, 1, 1],
                    "raw_hash": raw_hash,
                }
            ],
        }
        if level is not None:
            clean_record["level"] = level
        clean_records.append(clean_record)

    clean_dir = clean_root / book_id
    raw_dir = raw_root / book_id
    clean_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    (clean_dir / "clean_content_list.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in clean_records),
        encoding="utf-8",
    )
    (raw_dir / "content_list.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in raw_records),
        encoding="utf-8",
    )


def _artifact_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_build_all_writes_deterministic_ordered_artifacts(tmp_path: Path) -> None:
    clean_root = tmp_path / "clean"
    raw_root = tmp_path / "raw"
    output_root = tmp_path / "embedding_artifacts" / "v1"
    _write_book(clean_root, raw_root, "02_book", "第二本", "beta content")
    _write_book(clean_root, raw_root, "01_book", "第一本", "alpha content")
    config = BuildConfig(
        clean_root=clean_root,
        raw_root=raw_root,
        output_root=output_root,
        chunk_size=20,
        chunk_overlap=2,
        tokenizer=WhitespaceTokenizer(),
    )

    book_result = build_book(config, "01_book")
    manifest = build_all(config)

    assert book_result.report["provenance_refs_validated"] == 3
    chunks = [json.loads(line) for line in (output_root / "chunks.jsonl").read_text().splitlines()]
    assert [chunk["book_id"] for chunk in chunks] == ["01_book", "02_book"]
    assert [chunk["chunk_index"] for chunk in chunks] == [0, 1]
    assert len({chunk["chunk_id"] for chunk in chunks}) == 2

    exclusions = [
        json.loads(line)
        for line in (output_root / "excluded_records.jsonl").read_text().splitlines()
    ]
    assert [item["book_id"] for item in exclusions] == ["01_book", "02_book"]
    assert {item["reason"] for item in exclusions} == {"image_without_caption"}

    quality = json.loads((output_root / "quality_report.json").read_text())
    assert quality["passed"] is True
    assert manifest["book_ids"] == ["01_book", "02_book"]
    assert manifest["parameters"] == {
        "chunk_overlap": 2,
        "chunk_size": 20,
        "tokenizer_id": "whitespace-v1",
    }
    assert (output_root / "reports" / "01_book.json").is_file()
    assert (output_root / "reports" / "02_book.json").is_file()

    first_build = _artifact_bytes(output_root)
    assert build_all(config) == manifest
    assert _artifact_bytes(output_root) == first_build


def test_quality_failure_keeps_previous_published_directory(tmp_path: Path) -> None:
    clean_root = tmp_path / "clean"
    raw_root = tmp_path / "raw"
    output_root = tmp_path / "v1"
    _write_book(clean_root, raw_root, "01_book", "第一本", "valid content", include_image=False)
    config = BuildConfig(
        clean_root=clean_root,
        raw_root=raw_root,
        output_root=output_root,
        chunk_size=20,
        chunk_overlap=2,
        tokenizer=WhitespaceTokenizer(),
    )
    build_all(config)
    published = _artifact_bytes(output_root)

    clean_path = clean_root / "01_book" / "clean_content_list.jsonl"
    rows = [json.loads(line) for line in clean_path.read_text().splitlines()]
    rows[1]["text"] = "images/page_2.png"
    clean_path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )

    with pytest.raises(BuildQualityError, match="质量门禁"):
        build_all(config)

    assert _artifact_bytes(output_root) == published
    assert not list(output_root.parent.glob(".v1.build-*"))
