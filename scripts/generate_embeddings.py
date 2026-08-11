#!/usr/bin/env python3
"""从已发布 Chunk Artifact 生成可恢复、可校验的 1024 维 Embedding Artifact。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

# 直接以文件路径运行时，先把项目根目录加入模块搜索路径。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.embedding_generation.artifacts import (
    ArtifactError,
    ArtifactStore,
    default_output_root,
    load_chunk_dataset,
)
from scripts.embedding_generation.client import EmbeddingClient, OpenAIEmbeddingClient
from scripts.embedding_generation.runner import EmbeddingRunner
from scripts.embedding_generation.settings import EmbeddingSettings, SettingsError

_EXPECTED_MODEL = "text-embedding-v4"
_EXPECTED_DIMENSIONS = 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        required=True,
        help="显式配置文件路径；不会复制或修改该文件",
    )
    parser.add_argument(
        "--chunk-root",
        type=Path,
        required=True,
        help="包含 chunks.jsonl 和 manifest.json 的阶段一 Artifact 根目录",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="阶段二输出目录；默认包含 build id、模型和维度",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="本轮最多选择约 N 条 Chunk，按稳定批次边界执行",
    )
    parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="重新执行已有 shard 中包含永久失败的批次",
    )
    parser.add_argument("--batch-size", type=int, help="覆盖 RAG_EMBEDDING_BATCH_SIZE")
    parser.add_argument("--concurrency", type=int, help="覆盖 RAG_EMBEDDING_CONCURRENCY")
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        help="覆盖 RAG_EMBEDDING_TIMEOUT_SECONDS",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        help="覆盖 RAG_EMBEDDING_MAX_ATTEMPTS",
    )
    parser.add_argument(
        "--requests-per-second",
        type=float,
        help="覆盖 RAG_EMBEDDING_REQUESTS_PER_SECOND；0 表示关闭额外限速",
    )
    parser.add_argument(
        "--price-per-million-tokens",
        help="覆盖可选费用单价；只用于估算，不硬编码供应商价格",
    )
    return parser


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    client_factory: Callable[[EmbeddingSettings], EmbeddingClient] = OpenAIEmbeddingClient,
) -> int:
    """装配本地 Artifact 流水线；配置或输入错误时安全返回非零退出码。"""
    args = _parser().parse_args(argv)
    overrides = {
        "batch_size": args.batch_size,
        "concurrency": args.concurrency,
        "timeout_seconds": args.timeout_seconds,
        "max_attempts": args.max_attempts,
        "requests_per_second": args.requests_per_second,
        "price_per_million_tokens": args.price_per_million_tokens,
    }
    try:
        settings = EmbeddingSettings.from_sources(
            env_file=args.env_file,
            environ=environ,
            overrides=overrides,
        )
        if settings.model != _EXPECTED_MODEL:
            raise SettingsError(
                f"阶段二固定模型为 {_EXPECTED_MODEL}，实际配置为 {settings.model}"
            )
        if settings.dimensions != _EXPECTED_DIMENSIONS:
            raise SettingsError(
                f"阶段二固定向量维度为 {_EXPECTED_DIMENSIONS}，"
                f"实际配置为 {settings.dimensions}"
            )
        dataset = load_chunk_dataset(args.chunk_root)
        output_root = args.output_root or default_output_root(
            args.chunk_root,
            build_id=dataset.build_id,
            model=settings.model,
            dimensions=settings.dimensions,
        )
        store = ArtifactStore.open(
            output_root,
            dataset=dataset,
            model=settings.model,
            dimensions=settings.dimensions,
            batch_size=settings.batch_size,
            price_per_million_tokens=settings.price_per_million_tokens,
        )
        client = client_factory(settings)
        summary = EmbeddingRunner(
            settings=settings,
            client=client,
            store=store,
        ).run(limit=args.limit, retry_failures=args.retry_failures)
    except (ArtifactError, SettingsError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Embedding 生成失败：{exc}", file=sys.stderr)
        return 1

    payload = {
        "artifact_id": summary.manifest["artifact_id"],
        "status": summary.manifest["status"],
        "profile_id": summary.manifest["profile_id"],
        "counts": summary.manifest["counts"],
        "completed_batches_this_run": summary.completed_batches,
        "deferred_batches": list(summary.deferred_batches),
        "output_root": str(output_root),
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if summary.deferred_batches:
        return 2
    if int(summary.manifest["counts"]["failed_chunks"]) > 0:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
