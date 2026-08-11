"""建立 raw OCR block 索引，并校验 clean source_refs 的精确可追溯性。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.embedding_chunks.schemas import CleanRecord, SourceRef


class ProvenanceError(ValueError):
    """clean 与 raw OCR 的精确溯源不一致。"""


def _raw_hash(record: dict[str, Any]) -> str:
    raw = record.get("raw", {})
    payload = json.dumps(raw, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RawSourceIndex:
    """按 `(source_page, block_index)` 唯一定位 raw OCR 记录。"""

    records: dict[tuple[int, int], dict[str, Any]]

    @classmethod
    def from_jsonl(cls, path: Path) -> RawSourceIndex:
        records: dict[tuple[int, int], dict[str, Any]] = {}
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ProvenanceError(f"{path}:{line_number}: raw 记录必须是对象")
                key = int(payload["source_page"]), int(payload["block_index"])
                if key in records:
                    raise ProvenanceError(f"{path}:{line_number}: raw 定位键重复 {key}")
                records[key] = payload
        return cls(records=records)

    def get(self, key: tuple[int, int]) -> dict[str, Any]:
        try:
            return self.records[key]
        except KeyError as exc:
            raise ProvenanceError(f"raw OCR 中未找到精确定位 {key}") from exc

    def validate(self, ref: SourceRef) -> None:
        record = self.get(ref.key)
        if _raw_hash(record) != ref.raw_hash:
            raise ProvenanceError(f"{ref.key} 的 raw_hash 不一致")
        checks = {
            "mineru_page_index": ref.mineru_page_index,
            "block_index": ref.block_index,
            "chunk_id": ref.source_chunk_id,
            "source_pdf": ref.source_pdf,
            "element_type": ref.element_type,
        }
        for field_name, expected in checks.items():
            if record.get(field_name) != expected:
                raise ProvenanceError(
                    f"{ref.key} 的 {field_name} 不一致：{record.get(field_name)!r} != {expected!r}"
                )

    def validate_record(self, record: CleanRecord) -> None:
        for ref in record.source_refs:
            self.validate(ref)
