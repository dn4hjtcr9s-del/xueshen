"""严格读取 clean JSONL；缺失精确溯源时立即拒绝继续构建。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from scripts.embedding_chunks.schemas import CleanRecord, SourceRef


class SourceReadError(ValueError):
    """clean JSONL 格式不满足构建要求。"""


def _parse_record(payload: dict[str, Any], sequence: int) -> CleanRecord:
    refs_payload = payload.get("source_refs")
    if not isinstance(refs_payload, list) or not refs_payload:
        raise SourceReadError("clean record 缺少非空 source_refs")
    try:
        refs = tuple(SourceRef.from_dict(ref) for ref in refs_payload)
        extra = payload.get("extra", {})
        if not isinstance(extra, dict):
            raise ValueError("extra 必须是对象")
        level_value = payload.get("level")
        source_page = int(payload["source_page"])
        return CleanRecord(
            book_id=str(payload["book_id"]),
            book_name=str(payload["book_name"]),
            grade_level=str(payload.get("grade_level", "未知")),
            source_page=source_page,
            source_page_end=int(payload.get("source_page_end", source_page)),
            section=str(payload["section"]),
            element_type=str(payload["element_type"]),
            text=str(payload.get("text", "")),
            extra=extra,
            source_refs=refs,
            level=int(level_value) if level_value is not None else None,
            sequence=sequence,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceReadError(f"clean record 字段无效：{exc}") from exc


def read_clean_records(path: Path) -> Iterator[CleanRecord]:
    """逐行解析 clean JSONL，并在错误中保留文件与行号。"""
    with path.open(encoding="utf-8") as handle:
        for sequence, line in enumerate(handle):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise SourceReadError("每行必须是 JSON 对象")
                yield _parse_record(payload, sequence)
            except (json.JSONDecodeError, SourceReadError) as exc:
                raise SourceReadError(f"{path}:{sequence + 1}: {exc}") from exc
