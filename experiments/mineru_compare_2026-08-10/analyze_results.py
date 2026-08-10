"""MinerU 对照实验结果分析器：汇总结构、公式、表格和可嵌入性指标。"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from statistics import mean, median
from typing import Any, Iterable


NOISE_TYPES = {
    "header",
    "footer",
    "page_header",
    "page_footer",
    "page_footnote",
    "page_number",
}
SEMANTIC_TYPES = {"text", "equation", "table", "image", "chart"}
SUSPICIOUS_FORMULA_COMMANDS = re.compile(r"\\(?:sharp|sf|boxplus|gtrless)\b")


class _TableParser(HTMLParser):
    """宽松解析 MinerU 输出的 table_body，保留行和单元格顺序。"""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._current_row: list[str] | None = None
        self._current_cell: list[str] | None = None
        self._cell_attrs: dict[str, str] = {}
        self.cell_spans: list[dict[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag == "tr":
            self._current_row = []
        elif tag in {"td", "th"}:
            if self._current_row is None:
                self._current_row = []
            self._current_cell = []
            self._cell_attrs = attrs_dict

    def handle_data(self, data: str) -> None:
        if self._current_cell is not None:
            self._current_cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._current_cell is not None:
            value = html.unescape("".join(self._current_cell)).strip()
            assert self._current_row is not None
            self._current_row.append(value)
            self.cell_spans.append(
                {
                    "rowspan": _positive_int(self._cell_attrs.get("rowspan")),
                    "colspan": _positive_int(self._cell_attrs.get("colspan")),
                }
            )
            self._current_cell = None
            self._cell_attrs = {}
        elif tag == "tr" and self._current_row is not None:
            if self._current_row:
                self.rows.append(self._current_row)
            self._current_row = None


def _positive_int(value: str | None) -> int:
    try:
        return max(1, int(value or "1"))
    except ValueError:
        return 1


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _first_match(directory: Path, pattern: str) -> Path | None:
    matches = sorted(directory.glob(pattern))
    return matches[0] if matches else None


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _iter_v2_top_level_items(value: Any) -> Iterable[dict[str, Any]]:
    """遍历 content_list_v2 中每页的顶层块，不把段落内部行内公式重复计入。"""
    if not isinstance(value, list):
        return
    for page in value:
        if isinstance(page, list):
            for item in page:
                if isinstance(item, dict) and "type" in item:
                    yield item
        elif isinstance(page, dict) and "type" in page:
            yield page


def _normalize_image_reference(reference: str) -> str:
    reference = reference.replace("\\", "/")
    if reference.startswith("images/"):
        return reference
    return f"images/{Path(reference).name}"


def _collect_image_references(value: Any) -> set[str]:
    references: set[str] = set()
    for item in _walk_dicts(value):
        for key in ("img_path", "image_path"):
            reference = item.get(key)
            if isinstance(reference, str) and reference:
                references.add(_normalize_image_reference(reference))
        image_source = item.get("image_source")
        if isinstance(image_source, dict):
            path = image_source.get("path")
            if isinstance(path, str) and path:
                references.add(_normalize_image_reference(path))
    return references


def _read_table(body: str) -> _TableParser:
    parser = _TableParser()
    parser.feed(body or "")
    parser.close()
    return parser


def _normalize_text(text: str) -> str:
    """移除 Markdown/LaTeX 中对模型差异不敏感的空白，保留实际字符差异。"""
    text = html.unescape(text or "")
    text = re.sub(r"\s+", "", text)
    text = text.replace("\\left", "").replace("\\right", "")
    text = text.replace("\\textstyle", "").replace("\\displaystyle", "")
    return text


def _strip_markup(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = re.sub(r"\$+", "", text)
    return text


def count_numeric_spacing_artifacts(text: str) -> int:
    """统计扫描 OCR 常见的数字断裂，如 ``1 . 2 3`` 和 ``2 0 2``。"""
    if not text:
        return 0
    # MinerU pipeline 常用 ``\ ``、``~`` 表示数字组间距，先统一为空格再统计。
    text = re.sub(r"\\(?:quad|qquad|,|;|:|!)\s*", " ", text)
    text = re.sub(r"\\\s+", " ", text)
    text = text.replace("~", " ")
    separated_digits = re.findall(r"(?=(?<!\\)\d\s+\d)", text)
    spaced_decimal = re.findall(r"\d\s*\.\s*\d", text)
    return len(separated_digits) + len(spaced_decimal)


def _formula_brace_imbalance(text: str) -> bool:
    text = re.sub(r"\\[{}]", "", text or "")
    return text.count("{") != text.count("}")


def _formula_stats(items: list[dict[str, Any]], layout: Any) -> dict[str, Any]:
    formulas = [item for item in items if item.get("type") == "equation"]
    formula_texts = [str(item.get("text", "")) for item in formulas]
    content_image_count = sum(bool(item.get("img_path")) for item in formulas)
    # layout.json 同时包含块级公式和内部 span；块级公式带 lines，span 只有 bbox。
    layout_equations = [
        item
        for item in _walk_dicts(layout)
        if item.get("type") in {"interline_equation", "equation"}
        and isinstance(item.get("lines"), list)
    ]
    layout_image_count = sum(
        bool(_collect_image_references(item)) for item in layout_equations
    )
    artifact_count = sum(count_numeric_spacing_artifacts(text) for text in formula_texts)
    suspicious_count = sum(len(SUSPICIOUS_FORMULA_COMMANDS.findall(text)) for text in formula_texts)
    return {
        "count": len(formulas),
        "formula_texts": formula_texts,
        "character_count": sum(len(text) for text in formula_texts),
        "content_list_image_count": content_image_count,
        "content_list_image_coverage": _rate(content_image_count, len(formulas)),
        "layout_equation_count": len(layout_equations),
        "layout_image_count": layout_image_count,
        "layout_image_coverage": _rate(layout_image_count, len(layout_equations)),
        "numeric_spacing_artifact_count": artifact_count,
        "numeric_spacing_artifact_rate_per_1000_chars": _rate(artifact_count * 1000, sum(len(text) for text in formula_texts)),
        "formula_blocks_with_numeric_spacing": sum(
            count_numeric_spacing_artifacts(text) > 0 for text in formula_texts
        ),
        "unbalanced_brace_count": sum(_formula_brace_imbalance(text) for text in formula_texts),
        "suspicious_command_count": suspicious_count,
    }


def _table_stats(items: list[dict[str, Any]]) -> tuple[dict[str, Any], list[list[str]]]:
    tables = [item for item in items if item.get("type") == "table"]
    cell_sequences: list[list[str]] = []
    row_count = 0
    cell_count = 0
    malformed_count = 0
    rowspan_count = 0
    colspan_count = 0
    for table in tables:
        parser = _read_table(str(table.get("table_body", "")))
        if not parser.rows:
            malformed_count += 1
        row_count += len(parser.rows)
        cells = [cell for row in parser.rows for cell in row]
        cell_sequences.append(cells)
        cell_count += len(cells)
        rowspan_count += sum(span["rowspan"] > 1 for span in parser.cell_spans)
        colspan_count += sum(span["colspan"] > 1 for span in parser.cell_spans)
    with_images = sum(bool(table.get("img_path")) for table in tables)
    return (
        {
            "count": len(tables),
            "with_image_count": with_images,
            "image_coverage": _rate(with_images, len(tables)),
            "row_count": row_count,
            "cell_count": cell_count,
            "malformed_body_count": malformed_count,
            "rowspan_cell_count": rowspan_count,
            "colspan_cell_count": colspan_count,
            "cell_sequences": cell_sequences,
        },
        cell_sequences,
    )


def _rate(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round(float(numerator) / float(denominator), 6)


def _mean_or_zero(values: list[float]) -> float:
    return round(mean(values), 6) if values else 0.0


def _weighted_mean(values: list[tuple[float, int]]) -> float:
    total_weight = sum(weight for _, weight in values)
    if not total_weight:
        return 0.0
    return round(sum(value * weight for value, weight in values) / total_weight, 6)


def _page_count_from_v2(v2: Any) -> int:
    return len(v2) if isinstance(v2, list) else 0


def _page_indexes(items: list[dict[str, Any]]) -> list[int]:
    return sorted({int(item["page_idx"]) for item in items if isinstance(item.get("page_idx"), int)})


def _extract_semantic_plain_text(items: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for item in items:
        item_type = item.get("type")
        if item_type in NOISE_TYPES:
            continue
        if item_type in {"text", "equation"}:
            chunks.append(str(item.get("text", "")))
        elif item_type == "table":
            parser = _read_table(str(item.get("table_body", "")))
            chunks.extend(cell for row in parser.rows for cell in row)
    return _normalize_text("".join(chunks))


def _v2_type_counts(v2: Any) -> Counter[str]:
    return Counter(item.get("type") for item in _iter_v2_top_level_items(v2))


def summarize_result_directory(result_dir: Path, expected_page_count: int | None = None) -> dict[str, Any]:
    """读取单个模型/样本目录并返回不依赖外部服务的结构化指标。"""
    content_list_path = _first_match(result_dir, "*_content_list.json")
    content_list_v2_path = _first_match(result_dir, "*_content_list_v2.json")
    if content_list_path is None:
        raise FileNotFoundError(f"缺少 content_list.json: {result_dir}")
    if content_list_v2_path is None:
        raise FileNotFoundError(f"缺少 content_list_v2.json: {result_dir}")
    items = _load_json(content_list_path)
    v2 = _load_json(content_list_v2_path)
    layout_path = result_dir / "layout.json"
    layout = _load_json(layout_path) if layout_path.exists() else {}
    markdown_path = result_dir / "full.md"
    markdown = markdown_path.read_text(encoding="utf-8", errors="replace") if markdown_path.exists() else ""
    image_dir = result_dir / "images"
    image_files = {
        str(path.relative_to(result_dir)).replace("\\", "/")
        for path in image_dir.rglob("*")
        if path.is_file()
    }
    content_refs = _collect_image_references(items)
    v2_refs = _collect_image_references(v2)
    layout_refs = _collect_image_references(layout)
    table_summary, table_cell_sequences = _table_stats(items)
    formula_summary = _formula_stats(items, layout)
    type_counts = Counter(str(item.get("type")) for item in items)
    v2_type_counts = _v2_type_counts(v2)
    pages = _page_indexes(items)
    page_count = _page_count_from_v2(v2) or (max(pages) + 1 if pages else 0)
    if expected_page_count is not None:
        missing_pages = sorted(set(range(expected_page_count)) - set(pages))
    else:
        missing_pages = []
    noise_counts = {key: type_counts.get(key, 0) for key in sorted(NOISE_TYPES)}
    noise_count = sum(noise_counts.values())
    semantic_count = sum(type_counts.get(key, 0) for key in SEMANTIC_TYPES)
    return {
        "result_dir": str(result_dir),
        "page_count": page_count,
        "expected_page_count": expected_page_count,
        "missing_page_indexes": missing_pages,
        "content_block_count": len(items),
        "content_type_counts": dict(sorted(type_counts.items())),
        "v2_top_level_type_counts": dict(sorted(v2_type_counts.items())),
        "markdown": {
            "character_count": len(markdown),
            "nonempty_line_count": sum(bool(line.strip()) for line in markdown.splitlines()),
        },
        "noise": {
            "counts": noise_counts,
            "block_count": noise_count,
            "ratio_of_content_blocks": _rate(noise_count, len(items)),
        },
        "embedding_noise": {
            "block_count": noise_count,
            "ratio_of_content_blocks": _rate(noise_count, len(items)),
        },
        "equations": formula_summary,
        "tables": table_summary,
        "images": {
            "file_count": len(image_files),
            "content_list_reference_count": len(content_refs),
            "content_list_missing_reference_count": len(content_refs - image_files),
            "missing_reference_count": len(content_refs - image_files),
            "content_list_unreferenced_file_count": len(image_files - content_refs),
            "unreferenced_file_count": len(image_files - content_refs),
            "content_list_v2_reference_count": len(v2_refs),
            "layout_reference_count": len(layout_refs),
            "layout_missing_reference_count": len(layout_refs - image_files),
        },
        "page_indexes": pages,
        "formula_texts": formula_summary["formula_texts"],
        "table_cell_sequences": table_cell_sequences,
        "plain_text": _extract_semantic_plain_text(items),
        "v2_page_count": _page_count_from_v2(v2),
        "semantic_block_count": semantic_count,
    }


def _sequence_similarity(left: str, right: str) -> float:
    return round(
        difflib.SequenceMatcher(None, _normalize_text(left), _normalize_text(right)).ratio(),
        6,
    )


def _pair_similarities(left: list[str], right: list[str]) -> list[float]:
    return [
        _sequence_similarity(_normalize_text(a), _normalize_text(b))
        for a, b in zip(left, right)
    ]


def compare_result_summaries(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    """按输出顺序对比两个模型，重点突出可嵌入文本和表格结构差异。"""
    formula_left = left.get("formula_texts", [])
    formula_right = right.get("formula_texts", [])
    table_left = left.get("table_cell_sequences", [])
    table_right = right.get("table_cell_sequences", [])
    formula_similarities = _pair_similarities(formula_left, formula_right)
    table_similarities = [
        _sequence_similarity(
            _normalize_text("|".join(a)),
            _normalize_text("|".join(b)),
        )
        for a, b in zip(table_left, table_right)
    ]
    return {
        "content_block_count_delta": right.get("content_block_count", 0) - left.get("content_block_count", 0),
        "markdown_character_count_delta": right.get("markdown", {}).get("character_count", 0)
        - left.get("markdown", {}).get("character_count", 0),
        "page_count_equal": left.get("page_count") == right.get("page_count"),
        "content_type_counts_equal": left.get("content_type_counts") == right.get("content_type_counts"),
        "formula_pair_count": min(len(formula_left), len(formula_right)),
        "formula_count_delta": len(formula_right) - len(formula_left),
        "formula_similarity_mean": _mean_or_zero(formula_similarities),
        "formula_similarity_median": round(median(formula_similarities), 6) if formula_similarities else 0.0,
        "formula_similarity_min": round(min(formula_similarities), 6) if formula_similarities else 0.0,
        "formula_exact_after_normalization_count": sum(
            _normalize_text(a) == _normalize_text(b) for a, b in zip(formula_left, formula_right)
        ),
        "table_pair_count": min(len(table_left), len(table_right)),
        "table_count_delta": len(table_right) - len(table_left),
        "table_similarity_mean": _mean_or_zero(table_similarities),
        "table_similarity_min": round(min(table_similarities), 6) if table_similarities else 0.0,
        "table_cell_count_delta": right.get("tables", {}).get("cell_count", 0)
        - left.get("tables", {}).get("cell_count", 0),
        "plain_text_similarity": _sequence_similarity(left.get("plain_text", ""), right.get("plain_text", "")),
        "numeric_spacing_artifact_delta": right.get("equations", {}).get("numeric_spacing_artifact_count", 0)
        - left.get("equations", {}).get("numeric_spacing_artifact_count", 0),
        "suspicious_command_delta": right.get("equations", {}).get("suspicious_command_count", 0)
        - left.get("equations", {}).get("suspicious_command_count", 0),
        "content_list_equation_image_coverage_delta": right.get("equations", {}).get("content_list_image_coverage", 0.0)
        - left.get("equations", {}).get("content_list_image_coverage", 0.0),
        "layout_equation_image_coverage_delta": right.get("equations", {}).get("layout_image_coverage", 0.0)
        - left.get("equations", {}).get("layout_image_coverage", 0.0),
    }


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    value = _load_json(path)
    if not isinstance(value, list):
        raise ValueError(f"样本清单必须是数组: {path}")
    return value


def _load_log(path: Path) -> dict[str, Any]:
    value = _load_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"实验日志必须是对象: {path}")
    return value


def build_metrics(experiment_dir: Path) -> dict[str, Any]:
    """构建整个对照实验的机器可读指标，不读取或输出 API 密钥。"""
    manifest = _load_manifest(experiment_dir / "sample_manifest.json")
    log = _load_log(experiment_dir / "experiment_log.json")
    result: dict[str, Any] = {
        "experiment_dir": str(experiment_dir),
        "configuration": {
            "language": "ch",
            "is_ocr": True,
            "enable_formula": True,
            "enable_table": True,
        },
        "models": {},
        "samples": {},
    }
    for model in ("pipeline", "vlm"):
        model_log = log.get("models", {}).get(model, {})
        result["models"][model] = {
            "batch_id": model_log.get("batch_id"),
            "states": [entry.get("state") for entry in model_log.get("final_results", [])],
            "all_done": all(entry.get("state") == "done" for entry in model_log.get("final_results", [])),
        }
    for sample in manifest:
        sample_id = sample["sample_id"]
        expected_pages = int(sample["page_count"])
        pipeline = summarize_result_directory(experiment_dir / "raw" / "pipeline" / sample_id, expected_pages)
        vlm = summarize_result_directory(experiment_dir / "raw" / "vlm" / sample_id, expected_pages)
        result["samples"][sample_id] = {
            "sample": sample,
            "models": {"pipeline": pipeline, "vlm": vlm},
            "comparison_pipeline_to_vlm": compare_result_summaries(pipeline, vlm),
        }
    result["aggregate"] = _build_aggregate(result["samples"])
    return result


def _build_aggregate(samples: dict[str, Any]) -> dict[str, Any]:
    aggregate: dict[str, Any] = {}
    for model in ("pipeline", "vlm"):
        summaries = [item["models"][model] for item in samples.values()]
        aggregate[model] = {
            "sample_count": len(summaries),
            "page_count": sum(item["page_count"] for item in summaries),
            "content_block_count": sum(item["content_block_count"] for item in summaries),
            "markdown_character_count": sum(item["markdown"]["character_count"] for item in summaries),
            "equation_count": sum(item["equations"]["count"] for item in summaries),
            "equation_character_count": sum(
                item["equations"]["character_count"] for item in summaries
            ),
            "table_count": sum(item["tables"]["count"] for item in summaries),
            "image_file_count": sum(item["images"]["file_count"] for item in summaries),
            "equation_numeric_spacing_artifact_count": sum(
                item["equations"]["numeric_spacing_artifact_count"] for item in summaries
            ),
            "equation_suspicious_command_count": sum(
                item["equations"]["suspicious_command_count"] for item in summaries
            ),
            "content_list_equation_image_coverage_mean": _weighted_mean(
                [
                    (
                        item["equations"]["content_list_image_coverage"],
                        item["equations"]["count"],
                    )
                    for item in summaries
                ]
            ),
            "layout_equation_image_coverage_mean": _weighted_mean(
                [
                    (
                        item["equations"]["layout_image_coverage"],
                        item["equations"]["layout_equation_count"],
                    )
                    for item in summaries
                ]
            ),
        }
    comparisons = [item["comparison_pipeline_to_vlm"] for item in samples.values()]
    aggregate["comparison"] = {
        "all_page_counts_equal": all(item["page_count_equal"] for item in comparisons),
        "all_content_type_counts_equal": all(item["content_type_counts_equal"] for item in comparisons),
        "formula_similarity_mean": _weighted_mean(
            [(item["formula_similarity_mean"], item["formula_pair_count"]) for item in comparisons]
        ),
        "table_similarity_mean": _weighted_mean(
            [(item["table_similarity_mean"], item["table_pair_count"]) for item in comparisons]
        ),
        "plain_text_similarity_mean": _mean_or_zero([item["plain_text_similarity"] for item in comparisons]),
        "pipeline_to_vlm_numeric_spacing_artifact_delta": sum(
            item["numeric_spacing_artifact_delta"] for item in comparisons
        ),
        "pipeline_to_vlm_suspicious_command_delta": sum(item["suspicious_command_delta"] for item in comparisons),
    }
    return aggregate


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.3f}"
    return str(value)


def render_report(metrics: dict[str, Any]) -> str:
    """将指标渲染成供人工复核的简明 Markdown 报告。"""
    lines = [
        "# MinerU 在线 API OCR 对照实验报告",
        "",
        "> 实验范围：4 类样本、每类 8 页；`pipeline` 与 `vlm` 各跑一遍，共 64 页模型输出。",
        "> 本报告只分析本地已下载结果，不会输出 `.env` 中的 API 密钥。",
        "",
        "## 1. 实验配置",
        "",
        "```json",
        json.dumps(metrics["configuration"], ensure_ascii=False, indent=2),
        "```",
        "",
        "| 模型 | 批次 ID | 任务状态 |",
        "|---|---|---|",
    ]
    for model in ("pipeline", "vlm"):
        model_info = metrics["models"][model]
        lines.append(
            f"| `{model}` | `{model_info.get('batch_id')}` | "
            f"`{'全部 done' if model_info.get('all_done') else '存在非 done'}` |"
        )
    lines += [
        "",
        "### 样本范围",
        "",
        "| 样本 | 原始资料 | 原 PDF 页码 | 样本页数 |",
        "|---|---|---:|---:|",
    ]
    for sample_id, item in metrics["samples"].items():
        sample = item["sample"]
        lines.append(
            f"| `{sample_id}` | {sample['source']} | "
            f"{sample['start_page']}-{sample['end_page']} | {sample['page_count']} |"
        )
    lines += [
        "",
        "## 2. 总体结构指标",
        "",
        "| 模型 | 页数 | 内容块 | Markdown 字符 | 公式块 | 表格块 | 数字断裂次数 | 可疑公式命令 | content_list 公式图片覆盖率 | layout 公式图片覆盖率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for model in ("pipeline", "vlm"):
        info = metrics["aggregate"][model]
        lines.append(
            "| `{model}` | {page_count} | {content_block_count} | {markdown_character_count} | "
            "{equation_count} | {table_count} | {spacing} | {suspicious} | {content_cov:.1%} | {layout_cov:.1%} |".format(
                model=model,
                page_count=info["page_count"],
                content_block_count=info["content_block_count"],
                markdown_character_count=info["markdown_character_count"],
                equation_count=info["equation_count"],
                table_count=info["table_count"],
                spacing=info["equation_numeric_spacing_artifact_count"],
                suspicious=info["equation_suspicious_command_count"],
                content_cov=info["content_list_equation_image_coverage_mean"],
                layout_cov=info["layout_equation_image_coverage_mean"],
            )
        )
    comparison = metrics["aggregate"]["comparison"]
    lines += [
        "",
        "## 3. 分样本对照",
        "",
        "| 样本 | 内容类型计数是否一致 | Markdown 字符差（VLM−pipeline） | 公式平均相似度 | 表格平均相似度 | 语义文本相似度 | 数字断裂差（VLM−pipeline） |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for sample_id, item in metrics["samples"].items():
        cmp = item["comparison_pipeline_to_vlm"]
        lines.append(
            f"| `{sample_id}` | {'是' if cmp['content_type_counts_equal'] else '否'} | "
            f"{cmp['markdown_character_count_delta']} | {cmp['formula_similarity_mean']:.3f} | "
            f"{cmp['table_similarity_mean']:.3f} | {cmp['plain_text_similarity']:.3f} | "
            f"{cmp['numeric_spacing_artifact_delta']:+d} |"
        )
    lines += [
        "",
        "总体上，两个模型在本实验中都覆盖了 32 个样本页；内容类型数量大体一致，但这不等价于内容正确率一致。" 
        f"两模型的语义文本归一化平均相似度为 **{comparison['plain_text_similarity_mean']:.3f}**，"
        f"公式平均相似度为 **{comparison['formula_similarity_mean']:.3f}**，表格平均相似度为 **{comparison['table_similarity_mean']:.3f}**。",
        "",
        "## 4. 重点发现",
        "",
        "### 4.1 公式：VLM 更适合作为 embedding 主文本，但数字断裂仍需单独清洗",
        "",
        f"- 两者都识别出 95 个独立公式块。公式 LaTeX 字符总量从 `pipeline` 的 {metrics['aggregate']['pipeline']['equation_character_count']} 降到 `vlm` 的 {metrics['aggregate']['vlm']['equation_character_count']}，且可疑命令从 {metrics['aggregate']['pipeline']['equation_suspicious_command_count']} 降为 {metrics['aggregate']['vlm']['equation_suspicious_command_count']}；VLM 的表达通常更紧凑。",
        f"- **数字断裂问题没有显著改善**：启发式计数为 `pipeline` {metrics['aggregate']['pipeline']['equation_numeric_spacing_artifact_count']} 次、`vlm` {metrics['aggregate']['vlm']['equation_numeric_spacing_artifact_count']} 次，差值仅为 **{comparison['pipeline_to_vlm_numeric_spacing_artifact_delta']:+d}**。长小数仍会出现 `1. 0 7 2 1 8 7 5`，必须在 embedding 前清洗或标记。",
        "- 代表性差异：`pipeline` 输出 `$0 { \\leqslant } { x } { \\leqslant } 2$`，`vlm` 输出 `$0 \\leqslant x \\leqslant 2$`；后者更适合公式检索和分词。",
        "- `pipeline` 在 `content_list.json` 中几乎为每个公式保留 `img_path`；`vlm` 的 `content_list.json` 公式块往往没有 `img_path`。",
        "- 这不表示 VLM 的公式截图不存在：`layout.json` 仍保留公式块的 `image_path`，因此后处理应以 `content_list_v2.json`/`full.md` 作为语义文本来源，以 `layout.json` 和 `images/` 作为视觉回溯来源。",
        "- 概率统计样本中，`pipeline` 出现了明显的 `\\sharp`、`\\sf` 等疑似识别污染；这类命令应在 embedding 前进入异常检测或人工复核队列。",
        "",
        "### 4.2 表格：两种模型都需要表格级质量门禁，不能只看 `table` 数量",
        "",
        "- 表格数量和图片覆盖率在大多数样本相同，但复杂合并单元格会导致列/行归属不同。",
        "- `vlm` 对概率统计样本第 4 页的复杂合并单元格表格明显优于 `pipeline`：它保留了 `rowspan` 以及合并后的频数/概率结构，而 `pipeline` 把多行数据压进了少数单元格。",
        "- 但在概率统计样本第 6 页，VLM 又出现反例：首列被拼成 `A_2:4.5310.219635.575227.0132`，把区间、频数和统计量重复连接进同一单元格；该页 `pipeline` 的行列边界反而更稳定。",
        "- 因此表格不能按模型一刀切。建议保留 `table_body` 原文和表格图片；embedding 前先按行列重建纯文本，并检查每行数值列数、空单元格异常、跨行单元格和同一数值是否在一行重复出现。",
        "",
        "### 4.3 图形与教材结构：两种模型的内容类型覆盖基本一致",
        "",
        "- 高中立体几何样本识别出 28 个 `image` 块，混合初中教材识别出 `image`/`chart`/`table` 等结构；模型间数量一致，适合作为图片资产保留。",
        "- 页眉、页脚、页码被单独标记，建议从 embedding 正文中过滤，但在原始归档中保留，以便页码定位和审计。",
        "",
        "## 5. 本轮结论与全量建议",
        "",
        "**推荐全量先采用 `vlm` 作为主 OCR 模型，但不是无条件全量接受 VLM Markdown。** 推荐落盘层次如下：",
        "",
        "1. 保存 MinerU 原始 ZIP 解压目录，至少保留 `full.md`、`content_list.json`、`content_list_v2.json`、`layout.json`、`images/` 和原始 PDF。",
        "2. embedding 主文本使用 `content_list_v2.json` 生成，优先保留标题、正文、例题、定义、定理、公式和表格；过滤页眉、页脚、页码。",
        "3. 公式保留两份：清洗后的 LaTeX 文本 + 页码/bbox/图片路径；不要只存 Markdown 中的公式字符串。",
        "4. 表格先生成结构化行文本，同时保留原始 `table_body` 与表格图片；发现列数突变、数字拼接或空值异常时标记为 `table_review`。",
        "5. 对 `pipeline` 与 `vlm` 的差异页暂不做自动模型替换；全量阶段先建立异常页清单，再针对异常页做二次 OCR 或人工抽检。",
        "",
        "### 5.1 进入全量前仍需确认的门槛",
        "",
        "- 至少抽检 20 页：纯扫描公式、复杂统计表、彩色几何图、混合教材各 5 页。",
        "- 对公式检查分式、积分、上下标、根式、矩阵和特殊符号；对表格检查合并单元格、统计量和值。",
        "- 若表格异常页比例不可接受，再评估“VLM 主跑 + pipeline 对表格风险页复跑”的混合策略；当前证据不足以直接宣称某一模型在所有表格上更可靠。",
        "",
        "## 6. 产物说明",
        "",
        "- `metrics.json`：机器可读的逐样本、逐模型指标。",
        "- `comparison_report.md`：本报告。",
        "- `raw/`：两种模型的原始下载和解压结果。",
        "- `input/`：上传前裁出的 4 份 8 页样本 PDF。",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 MinerU OCR 对照实验指标和报告")
    parser.add_argument("--experiment-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    metrics = build_metrics(args.experiment_dir)
    metrics_path = args.experiment_dir / "metrics.json"
    report_path = args.experiment_dir / "comparison_report.md"
    metrics_path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_report(metrics), encoding="utf-8")
    print(f"已生成: {metrics_path}")
    print(f"已生成: {report_path}")


if __name__ == "__main__":
    main()
