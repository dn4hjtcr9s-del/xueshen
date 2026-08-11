"""Embedding Artifact 测试：覆盖输入校验、原子 shard、恢复和最终发布。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.embedding_generation.artifacts import (
    ArtifactError,
    ArtifactStore,
    load_chunk_dataset,
)
from scripts.embedding_generation.schemas import BatchOutcome, UsageStats


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_chunk_root(root: Path, *, duplicate_id: bool = False) -> Path:
    root.mkdir(parents=True)
    chunks = [
        {
            "schema_version": "embedding-chunks/v1",
            "chunk_id": "chunk-0",
            "chunk_index": 0,
            "content_hash": "content-0",
            "embedding_text": "函数的定义",
        },
        {
            "schema_version": "embedding-chunks/v1",
            "chunk_id": "chunk-0" if duplicate_id else "chunk-1",
            "chunk_index": 1,
            "content_hash": "content-1",
            "embedding_text": "函数的性质",
        },
        {
            "schema_version": "embedding-chunks/v1",
            "chunk_id": "chunk-2",
            "chunk_index": 2,
            "content_hash": "content-2",
            "embedding_text": "函数的例题",
        },
    ]
    chunks_path = root / "chunks.jsonl"
    chunks_path.write_text(
        "".join(json.dumps(chunk, ensure_ascii=False) + "\n" for chunk in chunks),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "embedding-chunks/v1",
        "build_id": "build-123",
        "chunk_count": len(chunks),
        "files": {
            "chunks.jsonl": {
                "records": len(chunks),
                "sha256": _sha256(chunks_path),
            }
        },
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return root


def _complete_batch(store: ArtifactStore, batch_index: int) -> BatchOutcome:
    plan = store.batches[batch_index]
    records = []
    for offset, job in enumerate(plan.jobs, start=1):
        records.extend(store.success_records(job, (float(offset), 0.0, 0.0)))
    return BatchOutcome(
        batch_index=plan.batch_index,
        batch_id=plan.batch_id,
        records=tuple(records),
        usage=UsageStats(prompt_tokens=len(plan.jobs) * 5, total_tokens=len(plan.jobs) * 5),
        request_count=1,
        retry_count=0,
        api_input_count=len(plan.jobs),
    )


def test_load_chunk_dataset_validates_manifest_hash_and_indexes(tmp_path: Path) -> None:
    chunk_root = _write_chunk_root(tmp_path / "chunks")

    dataset = load_chunk_dataset(chunk_root)

    assert dataset.build_id == "build-123"
    assert dataset.chunk_count == 3
    assert [chunk.chunk_index for chunk in dataset.chunks] == [0, 1, 2]
    assert dataset.chunks_sha256 == _sha256(chunk_root / "chunks.jsonl")
    assert dataset.manifest_sha256 == _sha256(chunk_root / "manifest.json")


def test_load_chunk_dataset_rejects_hash_mismatch(tmp_path: Path) -> None:
    chunk_root = _write_chunk_root(tmp_path / "chunks")
    with (chunk_root / "chunks.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("{}\n")

    with pytest.raises(ArtifactError, match="SHA-256"):
        load_chunk_dataset(chunk_root)


def test_load_chunk_dataset_rejects_duplicate_chunk_id(tmp_path: Path) -> None:
    chunk_root = _write_chunk_root(tmp_path / "chunks", duplicate_id=True)

    with pytest.raises(ArtifactError, match="重复 chunk_id"):
        load_chunk_dataset(chunk_root)


def test_artifact_store_refuses_profile_mismatch(tmp_path: Path) -> None:
    dataset = load_chunk_dataset(_write_chunk_root(tmp_path / "chunks"))
    output = tmp_path / "embeddings"
    ArtifactStore.open(
        output,
        dataset=dataset,
        model="text-embedding-v4",
        dimensions=3,
        batch_size=2,
    )

    with pytest.raises(ArtifactError, match="profile"):
        ArtifactStore.open(
            output,
            dataset=dataset,
            model="text-embedding-v4",
            dimensions=4,
            batch_size=2,
        )


def test_shards_resume_and_publish_ready_manifest(tmp_path: Path) -> None:
    dataset = load_chunk_dataset(_write_chunk_root(tmp_path / "chunks"))
    output = tmp_path / "embeddings"
    store = ArtifactStore.open(
        output,
        dataset=dataset,
        model="text-embedding-v4",
        dimensions=3,
        batch_size=2,
    )
    assert len(store.batches) == 2

    store.write_batch(_complete_batch(store, 0))
    partial = store.publish_summary()

    assert partial["status"] == "partial"
    assert partial["counts"] == {
        "expected_chunks": 3,
        "successful_chunks": 2,
        "failed_chunks": 0,
        "pending_chunks": 1,
        "completed_batches": 1,
        "total_batches": 2,
    }
    assert set(store.load_completed_batches()) == {0}

    store.write_batch(_complete_batch(store, 1))
    ready = store.publish_summary()

    assert ready["status"] == "ready"
    assert ready["counts"]["successful_chunks"] == 3
    assert ready["counts"]["pending_chunks"] == 0
    records = [json.loads(line) for line in (output / "embeddings.jsonl").read_text().splitlines()]
    assert [record["chunk_index"] for record in records] == [0, 1, 2]
    assert all(record["dimensions"] == 3 for record in records)
    assert ready["files"]["embeddings.jsonl"]["sha256"] == _sha256(
        output / "embeddings.jsonl"
    )


def test_temporary_shard_is_not_treated_as_completed(tmp_path: Path) -> None:
    dataset = load_chunk_dataset(_write_chunk_root(tmp_path / "chunks"))
    store = ArtifactStore.open(
        tmp_path / "embeddings",
        dataset=dataset,
        model="text-embedding-v4",
        dimensions=3,
        batch_size=2,
    )
    temporary = store.parts_dir / ".batch-000000.jsonl.interrupted.tmp"
    temporary.write_text('{"record_type":"batch"}\n', encoding="utf-8")

    assert store.load_completed_batches() == {}


def test_corrupt_batch_identity_is_rejected(tmp_path: Path) -> None:
    dataset = load_chunk_dataset(_write_chunk_root(tmp_path / "chunks"))
    store = ArtifactStore.open(
        tmp_path / "embeddings",
        dataset=dataset,
        model="text-embedding-v4",
        dimensions=3,
        batch_size=2,
    )
    store.write_batch(_complete_batch(store, 0))
    path = store.parts_dir / "batch-000000.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    metadata = json.loads(lines[0])
    metadata["batch_id"] = "wrong"
    path.write_text(
        "\n".join([json.dumps(metadata), *lines[1:]]) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ArtifactError, match="batch_id"):
        store.load_completed_batches()


def test_failure_record_keeps_manifest_partial_and_is_published(tmp_path: Path) -> None:
    dataset = load_chunk_dataset(_write_chunk_root(tmp_path / "chunks"))
    store = ArtifactStore.open(
        tmp_path / "embeddings",
        dataset=dataset,
        model="text-embedding-v4",
        dimensions=3,
        batch_size=2,
    )
    first_plan = store.batches[0]
    first_job, second_job = first_plan.jobs
    outcome = BatchOutcome(
        batch_index=first_plan.batch_index,
        batch_id=first_plan.batch_id,
        records=(
            *store.success_records(first_job, (1.0, 0.0, 0.0)),
            *store.failure_records(
                second_job,
                error_code="http_400",
                error_message="bad input",
                attempts=1,
            ),
        ),
        usage=UsageStats(prompt_tokens=3, total_tokens=3),
        request_count=2,
        retry_count=0,
        api_input_count=2,
    )
    store.write_batch(outcome)

    manifest = store.publish_summary()

    assert manifest["status"] == "partial"
    assert manifest["counts"]["successful_chunks"] == 1
    assert manifest["counts"]["failed_chunks"] == 1
    failures = [
        json.loads(line)
        for line in (store.output_root / "failures.jsonl").read_text().splitlines()
    ]
    assert failures[0]["error_code"] == "http_400"
    assert "vector" not in failures[0]
