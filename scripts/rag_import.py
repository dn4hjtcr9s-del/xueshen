"""RAG artifact 导入 CLI：必须显式传入 chunk 和 embedding artifact 路径。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

# 允许从任意工作目录以文件路径直接运行该 CLI。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.rag.artifact_loader import ArtifactValidationError, validate_artifacts
from backend.rag.importer import RAGImportError, import_artifacts
from backend.rag.settings import get_rag_settings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chunk-root", type=Path, required=True)
    parser.add_argument("--embedding-root", type=Path, required=True)
    parser.add_argument("--no-activate", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """校验 artifact 并执行独立 RAG 导入。"""
    args = _parser().parse_args(argv)
    try:
        settings = get_rag_settings()
        bundle = validate_artifacts(
            args.chunk_root,
            args.embedding_root,
            expected_dimensions=settings.embedding_dimensions,
            expected_model=settings.embedding_model,
        )
        result = import_artifacts(
            bundle,
            settings=settings,
            activate=not args.no_activate,
        )
    except (ArtifactValidationError, RAGImportError, OSError, ValueError) as exc:
        print(f"RAG 导入失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(asdict(result), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
