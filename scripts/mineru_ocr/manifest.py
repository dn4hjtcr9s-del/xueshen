"""全量 OCR 清单与 PDF 分片：保持书籍、分片和原始页码的一一对应。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

SCHEMA_VERSION = 1
DEFAULT_MAX_PAGES = 180


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """分块计算文件摘要，避免一次性把大 PDF 读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while data := stream.read(chunk_size):
            digest.update(data)
    return digest.hexdigest()


def _slug_stem(path: Path) -> str:
    """将书名转换为稳定、可读且适合目录的片段。"""
    name = unicodedata.normalize("NFKC", path.stem)
    name = re.sub(r"\s*\([^)]*(?:z-library|z-lib|1lib)[^)]*\)", "", name, flags=re.I)
    name = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", name, flags=re.UNICODE)
    name = re.sub(r"_+", "_", name).strip("_.")
    return name or "book"


def _load_existing(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _existing_book_ids(existing: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for book in existing.get("books", []):
        if not isinstance(book, dict):
            continue
        filename = book.get("source_filename")
        book_id = book.get("book_id")
        if isinstance(filename, str) and isinstance(book_id, str):
            result[filename] = book_id
    return result


def _existing_chunk_map(existing_book: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    result: dict[tuple[int, int], dict[str, Any]] = {}
    for chunk in existing_book.get("chunks", []):
        if not isinstance(chunk, dict):
            continue
        try:
            key = (int(chunk["page_start"]), int(chunk["page_end"]))
        except (KeyError, TypeError, ValueError):
            continue
        result[key] = chunk
    return result


def _book_id(filename: str, index: int, existing_ids: dict[str, str]) -> str:
    if filename in existing_ids:
        return existing_ids[filename]
    return f"{index:02d}_{_slug_stem(Path(filename))}"


def _make_chunks(
    *,
    book_id: str,
    source_filename: str,
    source_path: Path,
    page_count: int,
    book_dir: Path,
    max_pages: int,
    existing_chunks: dict[tuple[int, int], dict[str, Any]],
) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    for index, start in enumerate(range(1, page_count + 1, max_pages), start=1):
        end = min(start + max_pages - 1, page_count)
        chunk_id = f"{index:04d}_pages_{start:04d}_{end:04d}"
        chunk_dir = book_dir / "chunks" / chunk_id
        old = existing_chunks.get((start, end), {})
        chunk = {
            "chunk_id": chunk_id,
            "data_id": f"{book_id}__{index:04d}",
            "source_filename": source_filename,
            "source_path": str(source_path),
            "page_start": start,
            "page_end": end,
            "page_count": end - start + 1,
            "pdf_path": str(chunk_dir / "upload.pdf"),
            "chunk_dir": str(chunk_dir),
            "sha256": old.get("sha256"),
            "status": old.get("status", "pending"),
            "batch_id": old.get("batch_id"),
            "error": old.get("error"),
            "attempt_count": int(old.get("attempt_count", 0) or 0),
        }
        chunks.append(chunk)
    return chunks


def build_manifest(
    math_dir: Path,
    output_dir: Path,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict[str, Any]:
    """扫描数学资料并建立可恢复的书籍/分片清单。"""
    if max_pages <= 0:
        raise ValueError("max_pages 必须大于 0")
    math_dir = math_dir.resolve()
    output_dir = output_dir.resolve()
    manifest_path = output_dir / "manifest.json"
    existing = _load_existing(manifest_path)
    existing_ids = _existing_book_ids(existing)
    existing_books = {
        book.get("source_filename"): book
        for book in existing.get("books", [])
        if isinstance(book, dict) and isinstance(book.get("source_filename"), str)
    }
    pdfs = sorted(math_dir.glob("*.pdf"), key=lambda path: path.name)
    if not pdfs:
        raise FileNotFoundError(f"未找到 PDF: {math_dir}")

    books: list[dict[str, Any]] = []
    for index, source_path in enumerate(pdfs, start=1):
        reader = PdfReader(str(source_path), strict=False)
        page_count = len(reader.pages)
        filename = source_path.name
        book_id = _book_id(filename, index, existing_ids)
        book_dir = output_dir / book_id
        chunks = _make_chunks(
            book_id=book_id,
            source_filename=filename,
            source_path=source_path,
            page_count=page_count,
            book_dir=book_dir,
            max_pages=max_pages,
            existing_chunks=_existing_chunk_map(existing_books.get(filename, {})),
        )
        old_book = existing_books.get(filename, {})
        books.append(
            {
                "book_id": book_id,
                "book_name": source_path.stem,
                "source_filename": filename,
                "source_path": str(source_path),
                "source_sha256": sha256_file(source_path),
                "page_count": page_count,
                "chunk_pages": max_pages,
                "book_dir": str(book_dir),
                "status": old_book.get("status", "pending"),
                "chunks": chunks,
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": existing.get("created_at", _utc_now()),
        "updated_at": _utc_now(),
        "source_dir": str(math_dir),
        "output_dir": str(output_dir),
        "configuration": {
            "model_version": "vlm",
            "language": "ch",
            "is_ocr": True,
            "enable_formula": True,
            "enable_table": True,
        },
        "max_pages_per_chunk": max_pages,
        "books": books,
    }


def materialize_pending_chunks(manifest: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """生成缺失或摘要不匹配的分片 PDF，并把状态推进到 prepared。"""
    for book in manifest.get("books", []):
        if not isinstance(book, dict):
            continue
        source_path = Path(str(book["source_path"]))
        reader = PdfReader(str(source_path), strict=False)
        for chunk in book.get("chunks", []):
            if not isinstance(chunk, dict):
                continue
            chunk_dir = Path(str(chunk["chunk_dir"]))
            chunk_dir.mkdir(parents=True, exist_ok=True)
            pdf_path = Path(str(chunk["pdf_path"]))
            expected_hash = chunk.get("sha256")
            if pdf_path.is_file() and expected_hash and sha256_file(pdf_path) == expected_hash:
                if chunk.get("status") in {None, "pending"}:
                    chunk["status"] = "prepared"
                continue
            writer = PdfWriter()
            start = int(chunk["page_start"]) - 1
            end = int(chunk["page_end"])
            for page in reader.pages[start:end]:
                writer.add_page(page)
            fd, temporary_name = tempfile.mkstemp(prefix="upload-", suffix=".pdf", dir=chunk_dir)
            os.close(fd)
            temporary_path = Path(temporary_name)
            try:
                with temporary_path.open("wb") as stream:
                    writer.write(stream)
                temporary_path.replace(pdf_path)
            finally:
                temporary_path.unlink(missing_ok=True)
            chunk["sha256"] = sha256_file(pdf_path)
            chunk["status"] = "prepared"
            chunk["error"] = None
        book["status"] = "prepared"
    manifest["updated_at"] = _utc_now()
    return manifest


def save_manifest_atomic(path: Path, manifest: dict[str, Any]) -> None:
    """原子写入 JSON 清单，避免中断时留下半个 manifest。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    os.close(fd)
    temporary_path = Path(temporary_name)
    try:
        temporary_path.write_text(payload, encoding="utf-8")
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)
