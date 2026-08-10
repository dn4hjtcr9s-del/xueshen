"""书籍级 OCR 合并：映射原书页码、隔离图片，并生成适合后续 embedding 的结构化文件。"""

from __future__ import annotations

import copy
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .client import atomic_write_json, validate_result_files

HEADER_FOOTER_TYPES = {"header", "footer", "page_header", "page_footer", "page_number"}
FORMULA_TYPES = {"equation", "equation_inline", "equation_interline", "inline_equation", "interline_equation"}
TABLE_TYPES = {"table", "table_body"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
    temporary.replace(path)


def _result_paths(raw_dir: Path) -> dict[str, Path]:
    content = sorted(raw_dir.glob("*_content_list.json"))
    content_v2 = sorted(raw_dir.glob("*_content_list_v2.json"))
    if not content or not content_v2:
        raise FileNotFoundError(f"MinerU content list 不完整: {raw_dir}")
    return {
        "content_list": content[0],
        "content_list_v2": content_v2[0],
        "layout": raw_dir / "layout.json",
        "full_md": raw_dir / "full.md",
    }


def _source_page(chunk: dict[str, Any], page_idx: int) -> int:
    return int(chunk["page_start"]) + page_idx


def _flatten_text_parts(parts: Any) -> str:
    if isinstance(parts, str):
        return parts
    if isinstance(parts, list):
        rendered: list[str] = []
        for part in parts:
            if isinstance(part, str):
                rendered.append(part)
                continue
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type", ""))
            content = part.get("content", "")
            if kind in {"equation_inline", "inline_equation"}:
                rendered.append(f"${content}$")
            else:
                rendered.append(_flatten_text_parts(content))
        return "".join(rendered).strip()
    if isinstance(parts, dict):
        for key in ("paragraph_content", "text", "content", "title"):
            if key in parts:
                return _flatten_text_parts(parts[key])
    return ""


def _block_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, dict):
        for key in ("paragraph_content", "text", "title_content", "content"):
            if key in content:
                text = _flatten_text_parts(content[key])
                if text:
                    return text
    for key in ("text", "content"):
        if key in block:
            text = _flatten_text_parts(block[key])
            if text:
                return text
    return ""


def _formula_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, dict):
        for key in ("math_content", "latex", "equation"):
            value = content.get(key)
            if isinstance(value, str):
                return value.strip()
    for key in ("text", "content"):
        value = block.get(key)
        if isinstance(value, str):
            return value.strip().strip("$").strip()
    return ""


def _table_content(block: dict[str, Any]) -> str:
    content = block.get("content")
    candidates: list[Any] = [content, block]
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in ("html", "table_body", "latex", "markdown", "text"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        elif isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _extract_inline_formulas(block: dict[str, Any]) -> list[str]:
    formulas: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        kind = str(value.get("type", ""))
        if kind in {"equation_inline", "inline_equation"}:
            content = value.get("content")
            if isinstance(content, str) and content.strip():
                formulas.append(content.strip())
        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(block.get("content"))
    return formulas


def _replace_image_paths(value: Any, mapping: dict[str, str]) -> Any:
    if isinstance(value, list):
        return [_replace_image_paths(item, mapping) for item in value]
    if isinstance(value, dict):
        return {key: _replace_image_paths(item, mapping) for key, item in value.items()}
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        if normalized in mapping:
            return mapping[normalized]
        if normalized.startswith("images/") and normalized[7:] in mapping:
            return mapping[normalized[7:]]
    return value


def _copy_chunk_images(chunk_id: str, raw_dir: Path, book_images: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    image_dir = raw_dir / "images"
    if not image_dir.is_dir():
        return mapping
    book_images.mkdir(parents=True, exist_ok=True)
    for source in sorted(path for path in image_dir.rglob("*") if path.is_file()):
        relative = source.relative_to(image_dir).as_posix()
        safe_relative = relative.replace("/", "__")
        destination_name = f"{chunk_id}__{safe_relative}"
        destination = book_images / destination_name
        shutil.copy2(source, destination)
        target = f"images/{destination_name}"
        mapping[relative] = target
        mapping[f"images/{relative}"] = target
    return mapping


def _formula_risks(formula: str) -> list[str]:
    risks: list[str] = []
    if not formula.strip():
        return ["empty_formula"]
    if formula.count("{") != formula.count("}"):
        risks.append("unbalanced_braces")
    # 数字被逐字符放入相邻 LaTeX 分组，常见于 OCR 将 123 识别成 1 2 3 的断裂现象。
    if re.search(r"\d\s+(?:\d\s+){1,}\d", formula) or re.search(r"\{\d\}\s*\{\d\}", formula):
        risks.append("possible_digit_fragmentation")
    if "\\frac" in formula and not re.search(r"\\frac\s*\{[^{}]+(?:\{[^{}]*\}[^{}]*)*\}\s*\{", formula):
        risks.append("suspicious_fraction")
    return risks


def _table_risks(table: str) -> list[str]:
    if not table.strip():
        return ["empty_table"]
    lowered = table.lower()
    if "<table" in lowered:
        rows = len(re.findall(r"<tr\b", lowered))
        cells = len(re.findall(r"<t[dh]\b", lowered))
        risks: list[str] = []
        if rows == 0:
            risks.append("table_without_rows")
        if cells == 0:
            risks.append("table_without_cells")
        return risks
    if "\\begin{tabular" in table and "\\end{tabular" not in table:
        return ["unclosed_tabular"]
    return []


def _record_base(book: dict[str, Any], chunk: dict[str, Any], page_idx: int) -> dict[str, Any]:
    return {
        "book_id": str(book["book_id"]),
        "book_name": str(book["book_name"]),
        "source_pdf": str(book["source_filename"]),
        "source_page": _source_page(chunk, page_idx),
        "chunk_id": str(chunk["chunk_id"]),
        "mineru_page_index": page_idx,
    }


def _block_to_markdown(block: dict[str, Any]) -> str:
    kind = str(block.get("type", ""))
    if kind in {"equation_interline", "interline_equation", "equation"}:
        formula = _formula_text(block)
        return f"$$\n{formula}\n$$" if formula else ""
    if kind in TABLE_TYPES:
        return _table_content(block)
    text = _block_text(block)
    if kind == "title" and text:
        level = 2
        content = block.get("content")
        if isinstance(content, dict):
            raw_level = content.get("level") or content.get("title_level")
            if isinstance(raw_level, int):
                level = max(1, min(6, raw_level))
        return f"{'#' * level} {text}"
    if kind in {"image", "figure"}:
        image = block.get("content")
        if isinstance(image, dict):
            source = image.get("image_source") or image
            if isinstance(source, dict) and isinstance(source.get("path"), str):
                return f"![图片]({source['path']})"
    return text


def validate_chunk_result(chunk: dict[str, Any], chunk_dir: Path) -> dict[str, Any]:
    """验证一个分片的 MinerU 结果，并返回其逐页原书页码映射。"""
    raw_dir = chunk_dir / "raw"
    files = validate_result_files(raw_dir)
    result: dict[str, Any] = {"valid": False, "files": files, "pages": [], "errors": []}
    if not files["valid"]:
        result["errors"].append("required_result_files_missing_or_invalid")
        return result
    paths = _result_paths(raw_dir)
    content_v2 = _load_json(paths["content_list_v2"])
    if not isinstance(content_v2, list) or not all(isinstance(page, list) for page in content_v2):
        result["errors"].append("content_list_v2_not_page_list")
        return result
    expected_pages = int(chunk["page_end"]) - int(chunk["page_start"]) + 1
    if len(content_v2) != expected_pages:
        result["errors"].append(f"page_count_mismatch:{len(content_v2)}!={expected_pages}")
        return result
    result["pages"] = [
        {"mineru_page_index": index, "source_page": _source_page(chunk, index), "block_count": len(page)}
        for index, page in enumerate(content_v2)
    ]
    result["valid"] = True
    result["paths"] = {name: str(path) for name, path in paths.items()}
    return result


def merge_book(book: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """合并一本书的所有 chunk；任何缺片都会阻止该书被标记为 merged。"""
    book_id = str(book["book_id"])
    book_dir = output_dir / book_id
    book_dir.mkdir(parents=True, exist_ok=True)
    image_dir = book_dir / "images"
    if image_dir.exists():
        for path in sorted(image_dir.rglob("*"), reverse=True):
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
    image_dir.mkdir(parents=True, exist_ok=True)

    chunks = sorted(book.get("chunks", []), key=lambda item: int(item["page_start"]))
    chunk_checks: list[dict[str, Any]] = []
    for chunk in chunks:
        enriched = {**chunk, "book_id": book_id, "book_name": book["book_name"]}
        check = validate_chunk_result(enriched, Path(str(chunk["chunk_dir"])))
        check["chunk_id"] = chunk["chunk_id"]
        chunk_checks.append(check)
    failures = [check for check in chunk_checks if not check["valid"]]
    if failures:
        summary = {
            "book_id": book_id,
            "book_name": book["book_name"],
            "status": "incomplete",
            "page_count": int(book["page_count"]),
            "chunk_count": len(chunks),
            "failed_chunks": failures,
            "updated_at": _utc_now(),
        }
        quality_dir = book_dir / "quality"
        quality_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(quality_dir / "summary.json", summary)
        return summary

    content_records: list[dict[str, Any]] = []
    formula_records: list[dict[str, Any]] = []
    table_records: list[dict[str, Any]] = []
    anomaly_records: list[dict[str, Any]] = []
    markdown_pages: list[str] = []
    seen_pages: list[int] = []

    for chunk in chunks:
        raw_dir = Path(str(chunk["chunk_dir"])) / "raw"
        paths = _result_paths(raw_dir)
        pages = _load_json(paths["content_list_v2"])
        image_mapping = _copy_chunk_images(str(chunk["chunk_id"]), raw_dir, image_dir)
        for page_idx, raw_page in enumerate(pages):
            source_page = _source_page(chunk, page_idx)
            seen_pages.append(source_page)
            page_markdown: list[str] = [f"<!-- source_page: {source_page}; chunk_id: {chunk['chunk_id']} -->"]
            for block_index, original_block in enumerate(raw_page):
                if not isinstance(original_block, dict):
                    continue
                block = _replace_image_paths(copy.deepcopy(original_block), image_mapping)
                kind = str(block.get("type", "unknown"))
                base = _record_base(book, chunk, page_idx)
                record = {
                    **base,
                    "block_index": block_index,
                    "element_type": "text" if kind == "paragraph" else kind,
                    "include_in_embedding": kind not in HEADER_FOOTER_TYPES,
                    "text": _block_text(block),
                    "raw": block,
                }
                content_records.append(record)
                markdown = _block_to_markdown(block)
                if markdown and record["include_in_embedding"]:
                    page_markdown.append(markdown)

                if kind in FORMULA_TYPES:
                    formula = _formula_text(block)
                    risks = _formula_risks(formula)
                    formula_record = {**base, "block_index": block_index, "formula": formula, "formula_type": kind, "risks": risks, "raw": block}
                    formula_records.append(formula_record)
                    for risk in risks:
                        anomaly_records.append({**base, "block_index": block_index, "category": "formula", "risk": risk})
                for inline_index, formula in enumerate(_extract_inline_formulas(block)):
                    risks = _formula_risks(formula)
                    formula_records.append({**base, "block_index": block_index, "inline_index": inline_index, "formula": formula, "formula_type": "equation_inline", "risks": risks})
                    for risk in risks:
                        anomaly_records.append({**base, "block_index": block_index, "inline_index": inline_index, "category": "formula", "risk": risk})
                if kind in TABLE_TYPES:
                    table = _table_content(block)
                    risks = _table_risks(table)
                    table_records.append({**base, "block_index": block_index, "table": table, "table_type": kind, "risks": risks, "raw": block})
                    for risk in risks:
                        anomaly_records.append({**base, "block_index": block_index, "category": "table", "risk": risk})
            markdown_pages.append("\n\n".join(part for part in page_markdown if part).strip())

    expected_pages = list(range(1, int(book["page_count"]) + 1))
    page_errors: list[str] = []
    if seen_pages != expected_pages:
        page_errors.append("source_pages_not_contiguous")
    for record in content_records + formula_records + table_records:
        if record["book_id"] != book_id:
            page_errors.append("cross_book_record")
            break
    if page_errors:
        summary = {
            "book_id": book_id,
            "book_name": book["book_name"],
            "status": "incomplete",
            "errors": page_errors,
            "updated_at": _utc_now(),
        }
        quality_dir = book_dir / "quality"
        quality_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(quality_dir / "summary.json", summary)
        return summary

    (book_dir / "full.md").write_text("\n\n---\n\n".join(markdown_pages).rstrip() + "\n", encoding="utf-8")
    _write_jsonl(book_dir / "content_list.jsonl", content_records)
    _write_jsonl(book_dir / "formulas.jsonl", formula_records)
    _write_jsonl(book_dir / "tables.jsonl", table_records)
    quality_dir = book_dir / "quality"
    quality_dir.mkdir(parents=True, exist_ok=True)
    _write_jsonl(quality_dir / "anomalies.jsonl", anomaly_records)
    summary = {
        "book_id": book_id,
        "book_name": book["book_name"],
        "source_pdf": book["source_filename"],
        "status": "merged",
        "page_count": int(book["page_count"]),
        "chunk_count": len(chunks),
        "content_record_count": len(content_records),
        "formula_count": len(formula_records),
        "table_count": len(table_records),
        "formula_risk_count": sum(1 for item in anomaly_records if item["category"] == "formula"),
        "table_risk_count": sum(1 for item in anomaly_records if item["category"] == "table"),
        "image_count": sum(1 for path in image_dir.rglob("*") if path.is_file()),
        "filtered_header_footer_count": sum(1 for item in content_records if not item["include_in_embedding"]),
        "updated_at": _utc_now(),
    }
    atomic_write_json(quality_dir / "summary.json", summary)
    atomic_write_json(
        book_dir / "book.json",
        {
            "book_id": book_id,
            "book_name": book["book_name"],
            "source_filename": book["source_filename"],
            "source_path": book.get("source_path"),
            "source_sha256": book.get("source_sha256"),
            "page_count": book["page_count"],
            "status": "merged",
        },
    )
    return summary


def merge_all_books(manifest: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    """逐书执行合并并写入全量汇总，绝不跨书读取 chunk。"""
    summaries = [merge_book(book, output_dir) for book in manifest.get("books", [])]
    result = {
        "status": "complete" if summaries and all(item["status"] == "merged" for item in summaries) else "incomplete",
        "book_count": len(summaries),
        "merged_books": sum(1 for item in summaries if item["status"] == "merged"),
        "incomplete_books": sum(1 for item in summaries if item["status"] != "merged"),
        "total_pages": sum(int(item.get("page_count", 0)) for item in summaries),
        "formula_risk_count": sum(int(item.get("formula_risk_count", 0)) for item in summaries),
        "table_risk_count": sum(int(item.get("table_risk_count", 0)) for item in summaries),
        "books": summaries,
        "updated_at": _utc_now(),
    }
    atomic_write_json(output_dir / "summary.json", result)
    return result
