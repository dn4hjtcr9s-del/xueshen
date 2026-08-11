"""验证 clean source_refs 能在 raw OCR 中精确命中且哈希一致。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.embedding_chunks.provenance import ProvenanceError, RawSourceIndex
from scripts.embedding_chunks.schemas import SourceRef


def _raw_record(raw_text: str = "函数") -> dict[str, Any]:
    raw = {
        "type": "paragraph",
        "content": {"paragraph_content": [{"type": "text", "content": raw_text}]},
        "bbox": [1, 2, 3, 4],
    }
    return {
        "source_page": 7,
        "mineru_page_index": 6,
        "block_index": 3,
        "chunk_id": "0001_pages",
        "source_pdf": "book.pdf",
        "element_type": "text",
        "raw": raw,
    }


def _source_ref(record: dict[str, Any]) -> SourceRef:
    payload = json.dumps(record["raw"], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return SourceRef(
        source_page=7,
        mineru_page_index=6,
        block_index=3,
        source_chunk_id="0001_pages",
        source_pdf="book.pdf",
        element_type="text",
        bbox=(1, 2, 3, 4),
        raw_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
    )


def test_raw_source_index_validates_exact_ref(tmp_path: Path) -> None:
    record = _raw_record()
    path = tmp_path / "content_list.jsonl"
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    index = RawSourceIndex.from_jsonl(path)

    index.validate(_source_ref(record))
    assert index.get((7, 3))["source_pdf"] == "book.pdf"


def test_raw_source_index_rejects_hash_mismatch(tmp_path: Path) -> None:
    record = _raw_record()
    path = tmp_path / "content_list.jsonl"
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    bad_ref = _source_ref(record)
    bad_ref = SourceRef(**{**bad_ref.to_dict(), "raw_hash": "0" * 64})

    with pytest.raises(ProvenanceError, match="raw_hash"):
        RawSourceIndex.from_jsonl(path).validate(bad_ref)


def test_raw_source_index_rejects_missing_exact_key(tmp_path: Path) -> None:
    record = _raw_record()
    path = tmp_path / "content_list.jsonl"
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
    ref = _source_ref(record)
    missing = SourceRef(**{**ref.to_dict(), "block_index": 99})

    with pytest.raises(ProvenanceError, match="未找到"):
        RawSourceIndex.from_jsonl(path).validate(missing)
