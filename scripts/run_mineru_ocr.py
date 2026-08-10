"""MinerU 全量 OCR 命令行入口：准备分片、执行/恢复 API 任务、合并和查看状态。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .mineru_ocr.client import MinerUClient, atomic_write_json, load_api_key
from .mineru_ocr.manifest import build_manifest, materialize_pending_chunks, save_manifest_atomic
from .mineru_ocr.merge import merge_all_books
from .mineru_ocr.runner import OCRRunner, status_summary

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MATH_DIR = PROJECT_ROOT / "math_text"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "ocr_text"


def build_parser() -> argparse.ArgumentParser:
    """构造命令行解析器，集中定义可恢复 OCR 的全部操作。"""
    parser = argparse.ArgumentParser(description="使用 MinerU 在线 API 对 math_text PDF 执行按书籍隔离的 OCR")
    parser.add_argument("--math-dir", type=Path, default=DEFAULT_MATH_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="扫描 PDF 并生成最多 180 页的上传分片")
    prepare.add_argument("--max-pages", type=int, default=180)

    run = subparsers.add_parser("run", help="恢复旧任务并提交所有未完成分片")
    run.add_argument("--batch-size", type=int, default=40)
    run.add_argument("--poll-interval", type=float, default=10)
    run.add_argument("--poll-timeout", type=float, default=24 * 60 * 60)

    resume = subparsers.add_parser("resume", help="恢复中断批次，并继续处理可重试分片")
    resume.add_argument("--batch-size", type=int, default=40)
    resume.add_argument("--poll-interval", type=float, default=10)
    resume.add_argument("--poll-timeout", type=float, default=24 * 60 * 60)

    subparsers.add_parser("merge", help="按书籍合并已下载的 MinerU 原始结果")

    status = subparsers.add_parser("status", help="查看书籍、页数和分片状态")
    status.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _load_manifest(output_dir: Path) -> dict[str, Any]:
    path = output_dir / "manifest.json"
    if not path.is_file():
        raise RuntimeError(f"OCR manifest 不存在，请先执行 prepare: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"OCR manifest 格式异常: {path}")
    return value


def _write_book_metadata(manifest: dict[str, Any], output_dir: Path) -> None:
    for book in manifest.get("books", []):
        if not isinstance(book, dict):
            continue
        book_dir = output_dir / str(book["book_id"])
        atomic_write_json(
            book_dir / "book.json",
            {
                "book_id": book["book_id"],
                "book_name": book["book_name"],
                "source_filename": book["source_filename"],
                "source_path": book["source_path"],
                "source_sha256": book["source_sha256"],
                "page_count": book["page_count"],
                "chunk_count": len(book.get("chunks", [])),
                "status": book.get("status", "prepared"),
            },
        )


def command_prepare(math_dir: Path, output_dir: Path, max_pages: int) -> dict[str, Any]:
    """生成稳定 manifest 和本地 PDF 分片。"""
    manifest = build_manifest(math_dir, output_dir, max_pages=max_pages)
    materialize_pending_chunks(manifest, output_dir)
    save_manifest_atomic(output_dir / "manifest.json", manifest)
    _write_book_metadata(manifest, output_dir)
    return status_summary(manifest)


def command_run(output_dir: Path, batch_size: int, poll_interval: float, poll_timeout: float) -> dict[str, Any]:
    """恢复历史批次后持续提交，直到没有 prepared/retry 分片。"""
    manifest = _load_manifest(output_dir)
    token = load_api_key(PROJECT_ROOT)
    runner = OCRRunner(
        output_dir,
        manifest,
        MinerUClient(token),
        poll_interval_seconds=poll_interval,
        poll_timeout_seconds=poll_timeout,
    )
    runner.run_until_idle(batch_size=batch_size)
    return status_summary(manifest)


def command_merge(output_dir: Path) -> dict[str, Any]:
    """合并所有结果，并把书籍状态同步回 manifest。"""
    manifest = _load_manifest(output_dir)
    summary = merge_all_books(manifest, output_dir)
    by_id = {item["book_id"]: item for item in summary.get("books", [])}
    for book in manifest.get("books", []):
        if isinstance(book, dict) and book.get("book_id") in by_id:
            book["status"] = by_id[book["book_id"]]["status"]
    save_manifest_atomic(output_dir / "manifest.json", manifest)
    return summary


def _print_status(summary: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(f"书籍: {summary.get('book_count', 0)}")
    print(f"总页数: {summary.get('total_pages', 0)}")
    print(f"分片: {summary.get('chunk_count', 0)}")
    print("分片状态:")
    for status, count in sorted((summary.get("chunk_status") or {}).items()):
        print(f"  {status}: {count}")


def main(argv: list[str] | None = None) -> int:
    """执行子命令并以非零退出码报告可诊断错误。"""
    args = build_parser().parse_args(argv)
    math_dir = args.math_dir.resolve()
    output_dir = args.output_dir.resolve()
    try:
        if args.command == "prepare":
            summary = command_prepare(math_dir, output_dir, args.max_pages)
            _print_status(summary, False)
        elif args.command in {"run", "resume"}:
            summary = command_run(output_dir, args.batch_size, args.poll_interval, args.poll_timeout)
            _print_status(summary, False)
        elif args.command == "merge":
            summary = command_merge(output_dir)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        elif args.command == "status":
            summary = status_summary(_load_manifest(output_dir))
            quality_path = output_dir / "summary.json"
            if quality_path.is_file():
                summary["merge_summary"] = json.loads(quality_path.read_text(encoding="utf-8"))
            _print_status(summary, args.as_json)
        else:
            raise AssertionError(f"未处理的命令: {args.command}")
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(f"OCR 命令失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
