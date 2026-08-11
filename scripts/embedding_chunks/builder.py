"""编排 clean/raw 校验、语义切块、质量门禁、报告和原子发布。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from scripts.embedding_chunks.chunker import chunk_semantic_unit
from scripts.embedding_chunks.identifiers import content_hash, source_hash, stable_chunk_id
from scripts.embedding_chunks.provenance import RawSourceIndex
from scripts.embedding_chunks.quality import QualityReport, validate_chunks
from scripts.embedding_chunks.schemas import ChunkDraft, ChunkRecord, ExcludedRecord
from scripts.embedding_chunks.semantic_units import build_semantic_units
from scripts.embedding_chunks.source_reader import read_clean_records
from scripts.embedding_chunks.tokenizer import TiktokenTokenizer, Tokenizer

SCHEMA_VERSION = "embedding-chunks/v1"


class BuildError(RuntimeError):
    """构建输入、配置或发布过程不满足要求。"""


class BuildQualityError(BuildError):
    """最终 chunks 未通过质量门禁。"""

    def __init__(self, report: QualityReport) -> None:
        self.report = report
        super().__init__(f"embedding chunks 质量门禁失败：{report.error_counts}")


@dataclass(frozen=True, slots=True)
class BuildConfig:
    """chunk 构建所需路径、固定切块参数和 tokenizer 配置。"""

    clean_root: Path
    raw_root: Path
    output_root: Path
    chunk_size: int = 800
    chunk_overlap: int = 100
    tokenizer_encoding: str = "cl100k_base"
    tokenizer: Tokenizer | None = None

    def __post_init__(self) -> None:
        if self.chunk_size <= 0:
            raise ValueError("chunk_size 必须大于 0")
        if self.chunk_overlap < 0 or self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须满足 0 <= overlap < chunk_size")


@dataclass(frozen=True, slots=True)
class BookBuildResult:
    """单本书的内存构建结果；只有整批质量通过后才写入正式目录。"""

    book_id: str
    chunks: tuple[ChunkRecord, ...]
    exclusions: tuple[ExcludedRecord, ...]
    report: dict[str, Any]


def _resolve_tokenizer(config: BuildConfig) -> Tokenizer:
    return config.tokenizer or TiktokenTokenizer(config.tokenizer_encoding)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_bytes(payload))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            )
            handle.write("\n")


def _draft_to_record(draft: ChunkDraft, chunk_index: int) -> ChunkRecord:
    if not draft.source_refs:
        raise BuildError(f"{draft.book_id} 的 chunk draft 缺少 source_refs")
    content_digest = content_hash(draft.content_text)
    source_digest = source_hash(draft.source_refs)
    pages = [ref.source_page for ref in draft.source_refs]
    return ChunkRecord(
        schema_version=SCHEMA_VERSION,
        chunk_id=stable_chunk_id(
            book_id=draft.book_id,
            chapter_path=draft.chapter_path,
            content_role=draft.content_role,
            content_hash_value=content_digest,
            source_hash_value=source_digest,
        ),
        chunk_index=chunk_index,
        book_id=draft.book_id,
        book_name=draft.book_name,
        grade_level=draft.grade_level,
        section=draft.section,
        content_text=draft.content_text,
        embedding_text=draft.embedding_text,
        chapter_path=draft.chapter_path,
        content_role=draft.content_role,
        retrieval_weight=draft.retrieval_weight,
        source_page_start=min(pages),
        source_page_end=max(pages),
        source_refs=draft.source_refs,
        token_count=draft.token_count,
        tokenizer_id=draft.tokenizer_id,
        content_hash=content_digest,
        source_hash=source_digest,
    )


def build_book(config: BuildConfig, book_id: str) -> BookBuildResult:
    """在内存中构建并报告单本书，同时严格验证每个 source ref。"""
    tokenizer = _resolve_tokenizer(config)
    clean_path = config.clean_root / book_id / "clean_content_list.jsonl"
    raw_path = config.raw_root / book_id / "content_list.jsonl"
    if not clean_path.is_file():
        raise BuildError(f"缺少 clean 输入：{clean_path}")
    if not raw_path.is_file():
        raise BuildError(f"缺少 raw 输入：{raw_path}")

    records = list(read_clean_records(clean_path))
    if not records:
        raise BuildError(f"clean 输入为空：{clean_path}")
    mismatched_ids = sorted({record.book_id for record in records if record.book_id != book_id})
    if mismatched_ids:
        raise BuildError(f"目录 book_id={book_id} 与记录不一致：{mismatched_ids}")

    raw_index = RawSourceIndex.from_jsonl(raw_path)
    for record in records:
        raw_index.validate_record(record)

    units, semantic_exclusions = build_semantic_units(records)
    drafts: list[ChunkDraft] = []
    exclusions = list(semantic_exclusions)
    for unit in units:
        unit_drafts, chunk_exclusions = chunk_semantic_unit(
            unit,
            tokenizer,
            chunk_size=config.chunk_size,
            overlap=config.chunk_overlap,
        )
        drafts.extend(unit_drafts)
        exclusions.extend(chunk_exclusions)

    chunks = tuple(_draft_to_record(draft, index) for index, draft in enumerate(drafts))
    token_counts = [chunk.token_count for chunk in chunks]
    report: dict[str, Any] = {
        "book_id": book_id,
        "book_name": records[0].book_name,
        "clean_record_count": len(records),
        "provenance_records_validated": len(records),
        "provenance_refs_validated": sum(len(record.source_refs) for record in records),
        "semantic_unit_count": len(units),
        "chunk_count": len(chunks),
        "excluded_count": len(exclusions),
        "exclusion_reasons": dict(
            sorted(Counter(item.reason for item in exclusions).items())
        ),
        "content_roles": dict(sorted(Counter(chunk.content_role for chunk in chunks).items())),
        "token_count": {
            "min": min(token_counts, default=0),
            "max": max(token_counts, default=0),
            "total": sum(token_counts),
        },
        "inputs": {
            "clean_sha256": _file_hash(clean_path),
            "raw_sha256": _file_hash(raw_path),
        },
    }
    return BookBuildResult(
        book_id=book_id,
        chunks=chunks,
        exclusions=tuple(exclusions),
        report=report,
    )


def _discover_book_ids(config: BuildConfig) -> list[str]:
    if not config.clean_root.is_dir():
        raise BuildError(f"clean root 不存在：{config.clean_root}")
    book_ids = sorted(
        path.parent.name
        for path in config.clean_root.glob("*/clean_content_list.jsonl")
        if path.is_file()
    )
    if not book_ids:
        raise BuildError(f"clean root 中没有可构建书籍：{config.clean_root}")
    return book_ids


def _quality_payload(
    report: QualityReport,
    results: list[BookBuildResult],
    exclusions: list[ExcludedRecord],
) -> dict[str, Any]:
    payload = report.to_dict()
    validated_refs = sum(
        int(result.report["provenance_refs_validated"]) for result in results
    )
    payload.update(
        {
            "book_count": len(results),
            "provenance_refs_validated": validated_refs,
            "source_ref_hit_rate": 1.0,
            "excluded_records": len(exclusions),
            "exclusion_reasons": dict(
                sorted(Counter(item.reason for item in exclusions).items())
            ),
        }
    )
    return payload


def _manifest_payload(
    *,
    config: BuildConfig,
    tokenizer: Tokenizer,
    results: list[BookBuildResult],
    chunks: list[ChunkRecord],
    exclusions: list[ExcludedRecord],
    stage_root: Path,
) -> dict[str, Any]:
    file_entries: dict[str, dict[str, Any]] = {}
    for path in sorted(stage_root.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        relative = str(path.relative_to(stage_root))
        entry: dict[str, Any] = {"sha256": _file_hash(path)}
        if relative == "chunks.jsonl":
            entry["records"] = len(chunks)
        elif relative == "excluded_records.jsonl":
            entry["records"] = len(exclusions)
        file_entries[relative] = entry

    core: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "book_ids": [result.book_id for result in results],
        "book_count": len(results),
        "chunk_count": len(chunks),
        "excluded_count": len(exclusions),
        "parameters": {
            "chunk_size": config.chunk_size,
            "chunk_overlap": config.chunk_overlap,
            "tokenizer_id": tokenizer.tokenizer_id,
        },
        "inputs": {result.book_id: result.report["inputs"] for result in results},
        "files": file_entries,
    }
    canonical = json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {**core, "build_id": hashlib.sha256(canonical.encode("utf-8")).hexdigest()}


def _publish_directory(stage_root: Path, output_root: Path) -> None:
    """质量通过后替换目录；替换失败时恢复上一版。"""
    output_root.parent.mkdir(parents=True, exist_ok=True)
    if not output_root.exists():
        os.replace(stage_root, output_root)
        return

    backup = output_root.parent / f".{output_root.name}.backup-{uuid4().hex}"
    os.replace(output_root, backup)
    try:
        os.replace(stage_root, output_root)
    except BaseException:
        os.replace(backup, output_root)
        raise
    shutil.rmtree(backup)


def build_selected(config: BuildConfig, book_ids: list[str]) -> dict[str, Any]:
    """构建指定书籍集合，并在全局质量通过后发布完整 v1 目录。"""
    selected = sorted(set(book_ids))
    if not selected:
        raise BuildError("没有指定要构建的书籍")
    tokenizer = _resolve_tokenizer(config)
    resolved_config = replace(config, tokenizer=tokenizer)
    results = [build_book(resolved_config, book_id) for book_id in selected]

    chunks: list[ChunkRecord] = []
    exclusions: list[ExcludedRecord] = []
    for result in results:
        for chunk in result.chunks:
            chunks.append(replace(chunk, chunk_index=len(chunks)))
        exclusions.extend(result.exclusions)
    if not chunks:
        raise BuildError("构建结果没有可发布 chunks")

    quality = validate_chunks(chunks, tokenizer, config.chunk_size)
    if not quality.passed:
        raise BuildQualityError(quality)
    quality_payload = _quality_payload(quality, results, exclusions)

    config.output_root.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(
        tempfile.mkdtemp(
            prefix=f".{config.output_root.name}.build-",
            dir=config.output_root.parent,
        )
    )
    try:
        _write_jsonl(stage_root / "chunks.jsonl", [chunk.to_dict() for chunk in chunks])
        _write_jsonl(
            stage_root / "excluded_records.jsonl",
            [item.to_dict() for item in exclusions],
        )
        for result in results:
            _write_json(stage_root / "reports" / f"{result.book_id}.json", result.report)
        _write_json(stage_root / "quality_report.json", quality_payload)
        manifest = _manifest_payload(
            config=config,
            tokenizer=tokenizer,
            results=results,
            chunks=chunks,
            exclusions=exclusions,
            stage_root=stage_root,
        )
        _write_json(stage_root / "manifest.json", manifest)
        _publish_directory(stage_root, config.output_root)
        return manifest
    finally:
        if stage_root.exists():
            shutil.rmtree(stage_root)


def build_all(config: BuildConfig) -> dict[str, Any]:
    """按 book_id 排序发现并构建 clean root 中的全部书籍。"""
    return build_selected(config, _discover_book_ids(config))
