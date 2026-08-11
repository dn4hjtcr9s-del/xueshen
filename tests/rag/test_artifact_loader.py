"""RAG artifact loader 测试：校验哈希、逐行关联和向量有效性。"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

import pytest

from backend.rag.artifact_loader import (
    ArtifactValidationError,
    iter_artifact_rows,
    validate_artifacts,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_artifacts(
    tmp_path: Path,
    *,
    vector: list[float] | None = None,
    model: str = "text-embedding-v4",
    content_role: str = "body",
    retrieval_weight: float = 1.0,
) -> tuple[Path, Path]:
    chunk_root = tmp_path / "chunks"
    embedding_root = tmp_path / "embeddings"
    chunk_root.mkdir()
    embedding_root.mkdir()

    chunk = {
        "schema_version": "embedding-chunks/v1",
        "chunk_id": "3350c816-192b-589f-8d96-fbb534b2d8cd",
        "chunk_index": 0,
        "book_id": "book-1",
        "book_name": "测试教材",
        "grade_level": "高中",
        "section": "正文",
        "content_text": "函数 $f(x)=x^2$ 的定义。",
        "embedding_text": "书名：测试教材\n\n函数 $f(x)=x^2$ 的定义。",
        "chapter_path": ["第一章", "函数"],
        "content_role": content_role,
        "retrieval_weight": retrieval_weight,
        "source_page_start": 12,
        "source_page_end": 12,
        "source_refs": [{"source_page": 12, "block_index": 3}],
        "token_count": 18,
        "tokenizer_id": "tiktoken:cl100k_base",
        "content_hash": "content-hash-1",
        "source_hash": "source-hash-1",
    }
    chunks_path = chunk_root / "chunks.jsonl"
    chunks_path.write_text(json.dumps(chunk, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_json(
        chunk_root / "manifest.json",
        {
            "schema_version": "embedding-chunks/v1",
            "build_id": "build-1",
            "book_count": 1,
            "chunk_count": 1,
            "files": {"chunks.jsonl": {"records": 1, "sha256": _sha256(chunks_path)}},
        },
    )

    embedding = {
        "chunk_id": chunk["chunk_id"],
        "chunk_index": 0,
        "content_hash": chunk["content_hash"],
        "embedding_input_hash": "input-hash-1",
        "profile_id": "profile-1",
        "model": model,
        "dimensions": 3,
        "vector": vector if vector is not None else [0.1, 0.2, 0.3],
    }
    embeddings_path = embedding_root / "embeddings.jsonl"
    embeddings_path.write_text(
        json.dumps(embedding, ensure_ascii=False, allow_nan=True) + "\n",
        encoding="utf-8",
    )
    failures_path = embedding_root / "failures.jsonl"
    failures_path.write_text("", encoding="utf-8")
    _write_json(embedding_root / "profile.json", {"profile_id": "profile-1"})
    _write_json(embedding_root / "usage.json", {"chunk_coverage_count": 1})
    chunk_manifest_hash = _sha256(chunk_root / "manifest.json")
    _write_json(
        embedding_root / "manifest.json",
        {
            "schema_version": "embedding-artifact/v1",
            "artifact_id": "artifact-1",
            "profile_id": "profile-1",
            "status": "ready",
            "counts": {
                "expected_chunks": 1,
                "successful_chunks": 1,
                "failed_chunks": 0,
                "pending_chunks": 0,
            },
            "embedding": {
                "model": model,
                "dimensions": 3,
                "input_field": "embedding_text",
                "encoding_format": "float",
            },
            "source": {
                "chunk_build_id": "build-1",
                "chunk_count": 1,
                "chunk_manifest_sha256": chunk_manifest_hash,
                "chunks_sha256": _sha256(chunks_path),
            },
            "files": {
                "embeddings.jsonl": {"records": 1, "sha256": _sha256(embeddings_path)},
                "failures.jsonl": {"records": 0, "sha256": _sha256(failures_path)},
                "profile.json": {"sha256": _sha256(embedding_root / "profile.json")},
                "usage.json": {"sha256": _sha256(embedding_root / "usage.json")},
            },
        },
    )
    return chunk_root, embedding_root


def test_validate_and_stream_ready_artifacts(tmp_path: Path) -> None:
    chunk_root, embedding_root = _write_artifacts(tmp_path)

    bundle = validate_artifacts(chunk_root, embedding_root, expected_dimensions=3)
    rows = list(iter_artifact_rows(bundle))

    assert bundle.chunk_build_id == "build-1"
    assert bundle.embedding_artifact_id == "artifact-1"
    assert bundle.expected_chunk_count == 1
    assert len(rows) == 1
    assert rows[0].chunk_id == "3350c816-192b-589f-8d96-fbb534b2d8cd"
    assert rows[0].embedding_input_hash == "input-hash-1"
    assert rows[0].embedding == (0.1, 0.2, 0.3)
    assert rows[0].source_page_start == 12


def test_validate_artifacts_rejects_tampered_chunks(tmp_path: Path) -> None:
    chunk_root, embedding_root = _write_artifacts(tmp_path)
    (chunk_root / "chunks.jsonl").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ArtifactValidationError, match=r"chunks.jsonl.*SHA-256"):
        validate_artifacts(chunk_root, embedding_root, expected_dimensions=3)


def test_validate_artifacts_rejects_unexpected_embedding_model(tmp_path: Path) -> None:
    chunk_root, embedding_root = _write_artifacts(tmp_path, model="unexpected-model")

    with pytest.raises(ArtifactValidationError, match="embedding 模型不匹配"):
        validate_artifacts(chunk_root, embedding_root, expected_dimensions=3)


@pytest.mark.parametrize(
    ("content_role", "retrieval_weight"),
    [("answer_key", 1.0), ("body", 0.65)],
)
def test_iter_artifact_rows_rejects_role_weight_mismatch(
    tmp_path: Path,
    content_role: str,
    retrieval_weight: float,
) -> None:
    chunk_root, embedding_root = _write_artifacts(
        tmp_path,
        content_role=content_role,
        retrieval_weight=retrieval_weight,
    )
    bundle = validate_artifacts(chunk_root, embedding_root, expected_dimensions=3)

    with pytest.raises(ArtifactValidationError, match="retrieval_weight"):
        list(iter_artifact_rows(bundle))


@pytest.mark.parametrize("invalid", [[0.1, 0.2], [math.nan, 0.2, 0.3], [0.0, 0.0, 0.0]])
def test_iter_artifact_rows_rejects_invalid_vectors(
    tmp_path: Path,
    invalid: list[float],
) -> None:
    chunk_root, embedding_root = _write_artifacts(tmp_path, vector=invalid)
    bundle = validate_artifacts(chunk_root, embedding_root, expected_dimensions=3)

    with pytest.raises(ArtifactValidationError, match="向量"):
        list(iter_artifact_rows(bundle))
