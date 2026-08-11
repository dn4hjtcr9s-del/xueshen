#!/usr/bin/env python3
"""命令行构建可追溯、token-aware 的教材 embedding chunks。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.embedding_chunks.builder import (
    BuildConfig,
    BuildError,
    build_all,
    build_selected,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--all", action="store_true", help="构建 clean root 中全部书籍")
    selection.add_argument(
        "--book",
        action="append",
        dest="book_ids",
        metavar="BOOK_ID",
        help="构建指定 book_id；可重复传入",
    )
    parser.add_argument("--clean-root", type=Path, default=_PROJECT_ROOT / "clean_text")
    parser.add_argument("--raw-root", type=Path, default=_PROJECT_ROOT / "ocr_text")
    parser.add_argument(
        "--output-root",
        type=Path,
        default=_PROJECT_ROOT / "embedding_artifacts" / "v1",
    )
    parser.add_argument("--chunk-size", type=int, default=800)
    parser.add_argument("--chunk-overlap", type=int, default=100)
    parser.add_argument("--tokenizer", default="cl100k_base", dest="tokenizer_encoding")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """解析 CLI 参数；构建失败时返回非零退出码且不覆盖旧产物。"""
    args = _parser().parse_args(argv)
    try:
        config = BuildConfig(
            clean_root=args.clean_root,
            raw_root=args.raw_root,
            output_root=args.output_root,
            chunk_size=args.chunk_size,
            chunk_overlap=args.chunk_overlap,
            tokenizer_encoding=args.tokenizer_encoding,
        )
        manifest = build_all(config) if args.all else build_selected(config, args.book_ids)
    except (BuildError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"构建失败：{exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "build_id": manifest["build_id"],
                "book_count": manifest["book_count"],
                "chunk_count": manifest["chunk_count"],
                "output_root": str(args.output_root),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
