"""Embedding Artifact 存储：校验 Chunk 输入并原子发布可恢复的批次与汇总。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from scripts.embedding_generation.schemas import (
    ArtifactRecord,
    BatchOutcome,
    BatchPlan,
    ChunkInput,
    EmbeddingJob,
    UsageStats,
)
from scripts.embedding_generation.validation import (
    VectorValidationError,
    embedding_cache_key,
    embedding_input_hash,
    validate_vector,
)

_CHUNK_SCHEMA = "embedding-chunks/v1"
_PROFILE_SCHEMA = "embedding-profile/v1"
_BATCH_SCHEMA = "embedding-batch/v1"
_ARTIFACT_SCHEMA = "embedding-artifact/v1"
_BATCH_NAME = re.compile(r"batch-(\d{6})\.jsonl$")


class ArtifactError(RuntimeError):
    """表示输入或既有 Artifact 不能被安全信任。"""


@dataclass(frozen=True, slots=True)
class ChunkDataset:
    """已完成 manifest/hash/计数校验的阶段一输入。"""

    root: Path
    build_id: str
    schema_version: str
    manifest_sha256: str
    chunks_sha256: str
    chunks: tuple[ChunkInput, ...]

    @property
    def chunk_count(self) -> int:
        return len(self.chunks)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_hash(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_lines(path: Path, lines: list[str]) -> None:
    """在同目录完成 flush、fsync 和 replace，避免留下半文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            for line in lines:
                stream.write(line)
                stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _atomic_write_json(path: Path, payload: Any) -> None:
    _atomic_write_lines(
        path,
        [json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)],
    )


def _atomic_write_jsonl(path: Path, payloads: list[dict[str, Any]]) -> None:
    _atomic_write_lines(
        path,
        [
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for payload in payloads
        ],
    )


def _load_json(path: Path, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"无法读取{context}：{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArtifactError(f"{context}必须是 JSON object：{path}")
    return payload


def load_chunk_dataset(chunk_root: Path) -> ChunkDataset:
    """读取阶段一 Chunk Artifact，并在任何 API 请求前完成完整一致性校验。"""
    manifest_path = chunk_root / "manifest.json"
    chunks_path = chunk_root / "chunks.jsonl"
    manifest = _load_json(manifest_path, "Chunk manifest")
    schema_version = str(manifest.get("schema_version", ""))
    if schema_version != _CHUNK_SCHEMA:
        raise ArtifactError(f"Chunk schema 不支持：{schema_version!r}")
    build_id = str(manifest.get("build_id", "")).strip()
    if not build_id:
        raise ArtifactError("Chunk manifest 缺少 build_id")

    files = manifest.get("files")
    file_entry = files.get("chunks.jsonl") if isinstance(files, dict) else None
    if not isinstance(file_entry, dict):
        raise ArtifactError("Chunk manifest 缺少 files.chunks.jsonl")
    expected_hash = str(file_entry.get("sha256", ""))
    actual_hash = _file_hash(chunks_path)
    if actual_hash != expected_hash:
        raise ArtifactError(
            f"chunks.jsonl SHA-256 不匹配：期望 {expected_hash}，实际 {actual_hash}"
        )

    chunks: list[ChunkInput] = []
    seen_ids: set[str] = set()
    try:
        with chunks_path.open(encoding="utf-8") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                line = raw_line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if not isinstance(payload, dict):
                    raise ArtifactError(f"chunks.jsonl 第 {line_number} 行不是 object")
                if str(payload.get("schema_version", "")) != _CHUNK_SCHEMA:
                    raise ArtifactError(f"chunks.jsonl 第 {line_number} 行 schema 不匹配")
                chunk = ChunkInput.from_dict(payload)
                if chunk.chunk_index != len(chunks):
                    raise ArtifactError(
                        f"chunk_index 不连续：期望 {len(chunks)}，实际 {chunk.chunk_index}"
                    )
                if chunk.chunk_id in seen_ids:
                    raise ArtifactError(f"重复 chunk_id：{chunk.chunk_id}")
                seen_ids.add(chunk.chunk_id)
                chunks.append(chunk)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ArtifactError):
            raise
        raise ArtifactError(f"无法解析 chunks.jsonl：{exc}") from exc

    expected_count = int(manifest.get("chunk_count", -1))
    file_count = int(file_entry.get("records", -1))
    if len(chunks) != expected_count or len(chunks) != file_count:
        raise ArtifactError(
            f"Chunk 记录数不匹配：实际 {len(chunks)}，manifest {expected_count}，files {file_count}"
        )
    return ChunkDataset(
        root=chunk_root,
        build_id=build_id,
        schema_version=schema_version,
        manifest_sha256=_file_hash(manifest_path),
        chunks_sha256=actual_hash,
        chunks=tuple(chunks),
    )


def default_output_root(chunk_root: Path, *, build_id: str, model: str, dimensions: int) -> Path:
    """生成包含 Chunk build、模型和维度的默认输出目录。"""
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "-", model).strip("-") or "embedding"
    return chunk_root / "embeddings" / f"{build_id[:12]}-{safe_model}-d{dimensions}"


class ArtifactStore:
    """管理单一 source/profile 的稳定批次、恢复状态和最终汇总文件。"""

    def __init__(
        self,
        output_root: Path,
        *,
        dataset: ChunkDataset,
        profile: dict[str, Any],
        price_per_million_tokens: Decimal | None,
    ) -> None:
        self.output_root = output_root
        self.parts_dir = output_root / "parts"
        self.dataset = dataset
        self.profile = profile
        self.profile_id = str(profile["profile_id"])
        self.model = str(profile["embedding"]["model"])
        self.dimensions = int(profile["embedding"]["dimensions"])
        self.batch_size = int(profile["execution"]["batch_size"])
        self.price_per_million_tokens = price_per_million_tokens
        self._chunk_by_id = {chunk.chunk_id: chunk for chunk in dataset.chunks}
        self.batches = self._plan_batches()
        self._batch_by_index = {batch.batch_index: batch for batch in self.batches}

    @classmethod
    def open(
        cls,
        output_root: Path,
        *,
        dataset: ChunkDataset,
        model: str,
        dimensions: int,
        batch_size: int,
        price_per_million_tokens: Decimal | None = None,
    ) -> ArtifactStore:
        """创建或严格重开 output；profile 任一字段变化都拒绝混用。"""
        semantic_profile: dict[str, Any] = {
            "schema_version": _PROFILE_SCHEMA,
            "source": {
                "chunk_build_id": dataset.build_id,
                "chunk_manifest_sha256": dataset.manifest_sha256,
                "chunks_sha256": dataset.chunks_sha256,
                "chunk_count": dataset.chunk_count,
            },
            "embedding": {
                "model": model,
                "dimensions": dimensions,
                "input_field": "embedding_text",
                "encoding_format": "float",
            },
        }
        profile = {
            **semantic_profile,
            "profile_id": _canonical_hash(semantic_profile),
            "execution": {"batch_size": batch_size},
        }
        profile_path = output_root / "profile.json"
        if profile_path.exists():
            existing = _load_json(profile_path, "Embedding profile")
            if existing != profile:
                raise ArtifactError("已有 Artifact profile 与本次 source/model/dim/batch 不一致")
        else:
            _atomic_write_json(profile_path, profile)
        (output_root / "parts").mkdir(parents=True, exist_ok=True)
        return cls(
            output_root,
            dataset=dataset,
            profile=profile,
            price_per_million_tokens=price_per_million_tokens,
        )

    def _plan_batches(self) -> tuple[BatchPlan, ...]:
        jobs_by_key: OrderedDict[str, list[ChunkInput]] = OrderedDict()
        job_values: dict[str, tuple[str, str, str]] = {}
        for chunk in self.dataset.chunks:
            input_hash = embedding_input_hash(chunk.embedding_text)
            cache_key = embedding_cache_key(
                content_hash=chunk.content_hash,
                input_hash=input_hash,
                model=self.model,
                dimensions=self.dimensions,
            )
            values = (input_hash, chunk.content_hash, chunk.embedding_text)
            if cache_key in job_values and job_values[cache_key] != values:
                raise ArtifactError(f"cache key 冲突：{cache_key}")
            job_values[cache_key] = values
            jobs_by_key.setdefault(cache_key, []).append(chunk)

        jobs = [
            EmbeddingJob(
                cache_key=cache_key,
                input_hash=job_values[cache_key][0],
                content_hash=job_values[cache_key][1],
                embedding_text=job_values[cache_key][2],
                chunks=tuple(chunks),
            )
            for cache_key, chunks in jobs_by_key.items()
        ]
        batches: list[BatchPlan] = []
        for start in range(0, len(jobs), self.batch_size):
            batch_index = len(batches)
            batch_jobs = tuple(jobs[start : start + self.batch_size])
            batch_id = _canonical_hash(
                {
                    "batch_index": batch_index,
                    "cache_keys": [job.cache_key for job in batch_jobs],
                    "profile_id": self.profile_id,
                }
            )
            batches.append(
                BatchPlan(
                    batch_index=batch_index,
                    batch_id=batch_id,
                    jobs=batch_jobs,
                )
            )
        return tuple(batches)

    def success_records(
        self,
        job: EmbeddingJob,
        vector: tuple[float, ...] | list[float],
    ) -> tuple[ArtifactRecord, ...]:
        """将一个唯一 API 结果展开到共享 cache key 的全部 Chunk。"""
        validated = validate_vector(vector, dimensions=self.dimensions)
        source_chunk_id = job.chunks[0].chunk_id
        return tuple(
            ArtifactRecord(
                status="success",
                chunk_id=chunk.chunk_id,
                chunk_index=chunk.chunk_index,
                content_hash=chunk.content_hash,
                embedding_input_hash=job.input_hash,
                cache_key=job.cache_key,
                profile_id=self.profile_id,
                model=self.model,
                dimensions=self.dimensions,
                vector=validated,
                cached_from_chunk_id=(
                    source_chunk_id if chunk.chunk_id != source_chunk_id else None
                ),
            )
            for chunk in job.chunks
        )

    def failure_records(
        self,
        job: EmbeddingJob,
        *,
        error_code: str,
        error_message: str,
        attempts: int,
    ) -> tuple[ArtifactRecord, ...]:
        """把单个唯一输入的永久失败展开到相关 Chunk，但不写入原文。"""
        safe_message = error_message[:1000]
        return tuple(
            ArtifactRecord(
                status="failed",
                chunk_id=chunk.chunk_id,
                chunk_index=chunk.chunk_index,
                content_hash=chunk.content_hash,
                embedding_input_hash=job.input_hash,
                cache_key=job.cache_key,
                profile_id=self.profile_id,
                model=self.model,
                dimensions=self.dimensions,
                error_code=error_code,
                error_message=safe_message,
                attempts=attempts,
            )
            for chunk in job.chunks
        )

    def _validate_record(self, record: ArtifactRecord, expected_ids: set[str]) -> None:
        if record.chunk_id not in expected_ids:
            raise ArtifactError(f"批次包含计划外 chunk_id：{record.chunk_id}")
        chunk = self._chunk_by_id[record.chunk_id]
        input_hash = embedding_input_hash(chunk.embedding_text)
        cache_key = embedding_cache_key(
            content_hash=chunk.content_hash,
            input_hash=input_hash,
            model=self.model,
            dimensions=self.dimensions,
        )
        if (
            record.chunk_index != chunk.chunk_index
            or record.content_hash != chunk.content_hash
            or record.embedding_input_hash != input_hash
            or record.cache_key != cache_key
            or record.profile_id != self.profile_id
            or record.model != self.model
            or record.dimensions != self.dimensions
        ):
            raise ArtifactError(f"Chunk {record.chunk_id} 的 shard identity 不一致")
        if record.status == "success":
            if record.vector is None:
                raise ArtifactError(f"Chunk {record.chunk_id} 的成功记录缺少 vector")
            try:
                validate_vector(record.vector, dimensions=self.dimensions)
            except VectorValidationError as exc:
                raise ArtifactError(f"Chunk {record.chunk_id} 的向量无效：{exc}") from exc
        elif not record.error_code:
            raise ArtifactError(f"Chunk {record.chunk_id} 的失败记录缺少 error_code")

    def write_batch(self, outcome: BatchOutcome) -> Path:
        """验证完整覆盖后，将批次元数据和结果原子写入单个 JSONL shard。"""
        plan = self._batch_by_index.get(outcome.batch_index)
        if plan is None:
            raise ArtifactError(f"未知 batch_index：{outcome.batch_index}")
        if outcome.batch_id != plan.batch_id:
            raise ArtifactError("BatchOutcome batch_id 与计划不一致")
        expected_ids = {chunk.chunk_id for job in plan.jobs for chunk in job.chunks}
        actual_ids = [record.chunk_id for record in outcome.records]
        if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != expected_ids:
            raise ArtifactError("BatchOutcome 未恰好覆盖计划内全部 Chunk")
        for record in outcome.records:
            self._validate_record(record, expected_ids)

        metadata: dict[str, Any] = {
            "record_type": "batch",
            "schema_version": _BATCH_SCHEMA,
            "batch_index": outcome.batch_index,
            "batch_id": outcome.batch_id,
            "profile_id": self.profile_id,
            "source_build_id": self.dataset.build_id,
            "record_count": len(outcome.records),
            "job_count": len(plan.jobs),
            "usage": outcome.usage.to_dict(),
            "request_count": outcome.request_count,
            "retry_count": outcome.retry_count,
            "api_input_count": outcome.api_input_count,
            "completed_at": _utc_now(),
        }
        path = self.parts_dir / f"batch-{outcome.batch_index:06d}.jsonl"
        _atomic_write_jsonl(
            path,
            [metadata, *(record.to_dict() for record in outcome.records)],
        )
        return path

    def _record_from_dict(self, payload: dict[str, Any]) -> ArtifactRecord:
        status = str(payload.get("status", ""))
        if status not in {"success", "failed"}:
            raise ArtifactError(f"Shard 包含未知 status：{status!r}")
        vector_value = payload.get("vector")
        vector: tuple[float, ...] | None = None
        if status == "success":
            if not isinstance(vector_value, list):
                raise ArtifactError("成功 shard 记录缺少 vector 数组")
            try:
                vector = validate_vector(vector_value, dimensions=self.dimensions)
            except VectorValidationError as exc:
                raise ArtifactError(f"Shard 向量无效：{exc}") from exc
        return ArtifactRecord(
            status="success" if status == "success" else "failed",
            chunk_id=str(payload.get("chunk_id", "")),
            chunk_index=int(payload.get("chunk_index", -1)),
            content_hash=str(payload.get("content_hash", "")),
            embedding_input_hash=str(payload.get("embedding_input_hash", "")),
            cache_key=str(payload.get("cache_key", "")),
            profile_id=str(payload.get("profile_id", "")),
            model=str(payload.get("model", "")),
            dimensions=int(payload.get("dimensions", -1)),
            vector=vector,
            cached_from_chunk_id=(
                str(payload["cached_from_chunk_id"])
                if payload.get("cached_from_chunk_id") is not None
                else None
            ),
            error_code=(
                str(payload["error_code"]) if payload.get("error_code") is not None else None
            ),
            error_message=(
                str(payload["error_message"]) if payload.get("error_message") is not None else None
            ),
            attempts=int(payload.get("attempts", 0)),
        )

    def _read_batch(self, path: Path, plan: BatchPlan) -> BatchOutcome:
        try:
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line]
            payloads = [json.loads(line) for line in lines]
        except (OSError, json.JSONDecodeError) as exc:
            raise ArtifactError(f"无法读取 shard {path}: {exc}") from exc
        if not payloads or not isinstance(payloads[0], dict):
            raise ArtifactError(f"Shard 缺少批次元数据：{path}")
        metadata = payloads[0]
        if (
            metadata.get("record_type") != "batch"
            or metadata.get("schema_version") != _BATCH_SCHEMA
            or int(metadata.get("batch_index", -1)) != plan.batch_index
            or str(metadata.get("batch_id", "")) != plan.batch_id
            or str(metadata.get("profile_id", "")) != self.profile_id
            or str(metadata.get("source_build_id", "")) != self.dataset.build_id
        ):
            raise ArtifactError(f"Shard batch_id/profile/source 不匹配：{path}")
        result_payloads = payloads[1:]
        if not all(isinstance(payload, dict) for payload in result_payloads):
            raise ArtifactError(f"Shard 结果行必须是 JSON object：{path}")
        records = tuple(
            self._record_from_dict(payload)
            for payload in result_payloads
            if isinstance(payload, dict)
        )
        if int(metadata.get("record_count", -1)) != len(records):
            raise ArtifactError(f"Shard record_count 不匹配：{path}")
        expected_ids = {chunk.chunk_id for job in plan.jobs for chunk in job.chunks}
        actual_ids = [record.chunk_id for record in records]
        if len(actual_ids) != len(set(actual_ids)) or set(actual_ids) != expected_ids:
            raise ArtifactError(f"Shard 未恰好覆盖批次 Chunk：{path}")
        for record in records:
            self._validate_record(record, expected_ids)

        usage_payload = metadata.get("usage")
        usage_dict = usage_payload if isinstance(usage_payload, dict) else {}
        return BatchOutcome(
            batch_index=plan.batch_index,
            batch_id=plan.batch_id,
            records=records,
            usage=UsageStats(
                prompt_tokens=int(usage_dict.get("prompt_tokens", 0)),
                total_tokens=int(usage_dict.get("total_tokens", 0)),
            ),
            request_count=int(metadata.get("request_count", 0)),
            retry_count=int(metadata.get("retry_count", 0)),
            api_input_count=int(metadata.get("api_input_count", 0)),
        )

    def load_completed_batches(
        self,
        *,
        retry_failures: bool = False,
    ) -> dict[int, BatchOutcome]:
        """只读取已原子发布且 identity 完全匹配的 shard。"""
        completed: dict[int, BatchOutcome] = {}
        for path in sorted(self.parts_dir.glob("batch-*.jsonl")):
            match = _BATCH_NAME.fullmatch(path.name)
            if match is None:
                continue
            batch_index = int(match.group(1))
            plan = self._batch_by_index.get(batch_index)
            if plan is None:
                raise ArtifactError(f"发现计划外 shard：{path}")
            outcome = self._read_batch(path, plan)
            if retry_failures and any(record.status == "failed" for record in outcome.records):
                continue
            completed[batch_index] = outcome
        return completed

    def publish_summary(self) -> dict[str, Any]:
        """从可信 shard 重建压实文件、usage 和最后发布的 manifest。"""
        completed = self.load_completed_batches()
        records: list[ArtifactRecord] = []
        usage = UsageStats()
        request_count = 0
        retry_count = 0
        api_input_count = 0
        for batch_index in sorted(completed):
            outcome = completed[batch_index]
            records.extend(outcome.records)
            usage = usage + outcome.usage
            request_count += outcome.request_count
            retry_count += outcome.retry_count
            api_input_count += outcome.api_input_count

        by_chunk: dict[str, ArtifactRecord] = {}
        for record in records:
            if record.chunk_id in by_chunk:
                raise ArtifactError(f"跨 shard 重复 chunk_id：{record.chunk_id}")
            by_chunk[record.chunk_id] = record
        successful = sorted(
            (record for record in records if record.status == "success"),
            key=lambda record: record.chunk_index,
        )
        failed = sorted(
            (record for record in records if record.status == "failed"),
            key=lambda record: record.chunk_index,
        )

        embedding_payloads = [
            {
                key: value
                for key, value in record.to_dict().items()
                if key not in {"record_type", "status", "attempts"}
            }
            for record in successful
        ]
        failure_payloads = [
            {
                key: value
                for key, value in record.to_dict().items()
                if key not in {"record_type", "status"}
            }
            for record in failed
        ]
        embeddings_path = self.output_root / "embeddings.jsonl"
        failures_path = self.output_root / "failures.jsonl"
        _atomic_write_jsonl(embeddings_path, embedding_payloads)
        _atomic_write_jsonl(failures_path, failure_payloads)

        covered_count = len(successful) + len(failed)
        pending_count = self.dataset.chunk_count - covered_count
        estimated_cost: str | None = None
        if self.price_per_million_tokens is not None:
            cost = Decimal(usage.total_tokens) * self.price_per_million_tokens / Decimal(1_000_000)
            estimated_cost = format(cost, "f")
        usage_payload: dict[str, Any] = {
            "schema_version": "embedding-usage/v1",
            "profile_id": self.profile_id,
            "prompt_tokens": usage.prompt_tokens,
            "total_tokens": usage.total_tokens,
            "request_count": request_count,
            "retry_count": retry_count,
            "api_input_count": api_input_count,
            "chunk_coverage_count": covered_count,
            "cache_reused_chunks": max(0, covered_count - api_input_count),
            "price_per_million_tokens": (
                str(self.price_per_million_tokens)
                if self.price_per_million_tokens is not None
                else None
            ),
            "estimated_cost": estimated_cost,
        }
        usage_path = self.output_root / "usage.json"
        _atomic_write_json(usage_path, usage_payload)

        counts = {
            "expected_chunks": self.dataset.chunk_count,
            "successful_chunks": len(successful),
            "failed_chunks": len(failed),
            "pending_chunks": pending_count,
            "completed_batches": len(completed),
            "total_batches": len(self.batches),
        }
        status = (
            "ready" if len(successful) == self.dataset.chunk_count and not failed else "partial"
        )
        files: dict[str, dict[str, Any]] = {
            "profile.json": {"sha256": _file_hash(self.output_root / "profile.json")},
            "embeddings.jsonl": {
                "sha256": _file_hash(embeddings_path),
                "records": len(successful),
            },
            "failures.jsonl": {
                "sha256": _file_hash(failures_path),
                "records": len(failed),
            },
            "usage.json": {"sha256": _file_hash(usage_path)},
        }
        core: dict[str, Any] = {
            "schema_version": _ARTIFACT_SCHEMA,
            "status": status,
            "profile_id": self.profile_id,
            "source": self.profile["source"],
            "embedding": self.profile["embedding"],
            "counts": counts,
            "files": files,
        }
        manifest = {
            **core,
            "artifact_id": _canonical_hash(core),
            "updated_at": _utc_now(),
        }
        _atomic_write_json(self.output_root / "manifest.json", manifest)
        return manifest
