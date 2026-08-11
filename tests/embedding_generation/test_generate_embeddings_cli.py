"""Embedding CLI 测试：覆盖直接执行、配置错误、小批运行和 secret 安全。"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from scripts.embedding_generation.schemas import ClientBatchResponse, UsageStats
from scripts.embedding_generation.settings import EmbeddingSettings
from scripts.generate_embeddings import main
from tests.embedding_generation.helpers import write_chunk_root


class FakeClient:
    """CLI 端到端测试使用的 1024 维客户端，不访问网络。"""

    def embed(self, texts: Sequence[str]) -> ClientBatchResponse:
        vectors = []
        for _ in texts:
            vector = [0.0] * 1024
            vector[0] = 1.0
            vectors.append(tuple(vector))
        return ClientBatchResponse(
            vectors=tuple(vectors),
            usage=UsageStats(prompt_tokens=len(texts), total_tokens=len(texts)),
        )


def _client_factory(_settings: EmbeddingSettings) -> FakeClient:
    return FakeClient()


def test_cli_script_can_be_executed_directly() -> None:
    project_root = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [sys.executable, "scripts/generate_embeddings.py", "--help"],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--env-file" in completed.stdout
    assert "--chunk-root" in completed.stdout
    assert "--retry-failures" in completed.stdout


def test_cli_returns_nonzero_when_api_key_is_missing(tmp_path: Path, capsys) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EMBEDDING_BASE_URL=https://example.invalid/v1\nEMBEDDING_MODEL=text-embedding-v4\n",
        encoding="utf-8",
    )
    chunk_root = write_chunk_root(tmp_path / "chunks", ["text"])

    exit_code = main(
        ["--env-file", str(env_file), "--chunk-root", str(chunk_root)],
        environ={},
        client_factory=_client_factory,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "API key" in captured.err


def test_cli_runs_limited_batch_without_leaking_secret(tmp_path: Path, capsys) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "EMBEDDING_BASE_URL=https://example.invalid/v1",
                "EMBEDDING_MODEL=text-embedding-v4",
                "DASHSCOPE_API_KEY=do-not-print-this",
                "RAG_EMBEDDING_BATCH_SIZE=1",
                "RAG_EMBEDDING_CONCURRENCY=1",
            ]
        ),
        encoding="utf-8",
    )
    chunk_root = write_chunk_root(tmp_path / "chunks", ["first", "second"])
    output_root = tmp_path / "output"

    exit_code = main(
        [
            "--env-file",
            str(env_file),
            "--chunk-root",
            str(chunk_root),
            "--output-root",
            str(output_root),
            "--limit",
            "1",
        ],
        environ={},
        client_factory=_client_factory,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "do-not-print-this" not in captured.out
    assert "do-not-print-this" not in captured.err
    summary = json.loads(captured.out)
    assert summary["status"] == "partial"
    assert summary["counts"]["successful_chunks"] == 1
    assert summary["counts"]["pending_chunks"] == 1
    assert summary["output_root"] == str(output_root)
    embedding = json.loads((output_root / "embeddings.jsonl").read_text().splitlines()[0])
    assert embedding["dimensions"] == 1024
    assert len(embedding["vector"]) == 1024


def test_cli_rejects_non_1024_production_dimension(tmp_path: Path, capsys) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "EMBEDDING_BASE_URL=https://example.invalid/v1",
                "EMBEDDING_MODEL=text-embedding-v4",
                "EMBEDDING_API_KEY=secret",
                "RAG_EMBEDDING_DIMENSIONS=512",
            ]
        ),
        encoding="utf-8",
    )
    chunk_root = write_chunk_root(tmp_path / "chunks", ["text"])

    exit_code = main(
        ["--env-file", str(env_file), "--chunk-root", str(chunk_root)],
        environ={},
        client_factory=_client_factory,
    )

    assert exit_code == 1
    assert "1024" in capsys.readouterr().err
