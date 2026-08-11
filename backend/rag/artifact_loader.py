"""RAG artifact 读取边界：在连接数据库前验证 manifest、哈希和逐条向量。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterator
from dataclasses import dataclass
from itertools import zip_longest
from pathlib import Path
from typing import Any


class ArtifactValidationError(ValueError):
    """阶段一或阶段二 artifact 不满足可安全入库条件。"""


@dataclass(frozen=True, slots=True)
class ArtifactBundle:
    """通过静态完整性校验的 artifact 路径和版本元数据。"""

    chunk_root: Path
    embedding_root: Path
    chunk_manifest: dict[str, Any]
    embedding_manifest: dict[str, Any]
    chunk_build_id: str
    embedding_artifact_id: str
    embedding_profile_id: str
    embedding_model: str
    embedding_dimensions: int
    expected_chunk_count: int
    chunk_manifest_sha256: str
    artifact_manifest_sha256: str
    chunks_sha256: str
    embeddings_sha256: str


@dataclass(frozen=True, slots=True)
class ArtifactRow:
    """由同一个 chunk 与 embedding 记录严格关联后的单条待入库数据。"""

    chunk_id: str
    chunk_index: int
    book_id: str
    book_name: str
    grade_level: str
    section: str
    chapter_path: tuple[str, ...]
    content_role: str
    retrieval_weight: float
    content_text: str
    embedding_text: str
    token_count: int
    tokenizer_id: str
    source_page_start: int
    source_page_end: int
    source_refs: tuple[dict[str, Any], ...]
    content_hash: str
    source_hash: str
    embedding_input_hash: str
    embedding: tuple[float, ...]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactValidationError(f"无法读取 {label}：{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ArtifactValidationError(f"{label} 必须是 JSON object：{path}")
    return payload


def _require_text(payload: dict[str, Any], key: str, *, label: str) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise ArtifactValidationError(f"{label}.{key} 不能为空")
    return value


def _verify_file(path: Path, expected_hash: object, *, label: str) -> str:
    if not path.is_file():
        raise ArtifactValidationError(f"缺少 {label}：{path}")
    actual = _sha256(path)
    if actual != str(expected_hash):
        raise ArtifactValidationError(
            f"{label} SHA-256 不匹配：expected={expected_hash}, actual={actual}"
        )
    return actual


def validate_artifacts(
    chunk_root: Path,
    embedding_root: Path,
    *,
    expected_dimensions: int = 1024,
    expected_model: str = "text-embedding-v4",
) -> ArtifactBundle:
    """验证两个 artifact 的静态文件、版本和数量声明，返回可流式读取的 bundle。"""
    chunk_root = chunk_root.expanduser().resolve()
    embedding_root = embedding_root.expanduser().resolve()
    chunk_manifest_path = chunk_root / "manifest.json"
    embedding_manifest_path = embedding_root / "manifest.json"
    chunk_manifest = _read_json(chunk_manifest_path, label="chunk manifest")
    embedding_manifest = _read_json(embedding_manifest_path, label="embedding manifest")

    if embedding_manifest.get("status") != "ready":
        raise ArtifactValidationError("embedding manifest.status 必须为 ready")

    chunk_build_id = _require_text(chunk_manifest, "build_id", label="chunk manifest")
    artifact_id = _require_text(embedding_manifest, "artifact_id", label="embedding manifest")
    profile_id = _require_text(embedding_manifest, "profile_id", label="embedding manifest")
    source = embedding_manifest.get("source")
    counts = embedding_manifest.get("counts")
    embedding = embedding_manifest.get("embedding")
    if (
        not isinstance(source, dict)
        or not isinstance(counts, dict)
        or not isinstance(embedding, dict)
    ):
        raise ArtifactValidationError("embedding manifest 缺少 source/counts/embedding object")

    if source.get("chunk_build_id") != chunk_build_id:
        raise ArtifactValidationError("embedding source.chunk_build_id 与 chunk manifest 不一致")
    chunk_count = int(chunk_manifest.get("chunk_count", -1))
    expected_count = int(counts.get("expected_chunks", -1))
    if (
        chunk_count < 0
        or expected_count != chunk_count
        or int(source.get("chunk_count", -1)) != chunk_count
    ):
        raise ArtifactValidationError("chunk_count 在两个 manifest 中不一致")
    if (
        int(counts.get("successful_chunks", -1)) != expected_count
        or int(counts.get("failed_chunks", -1)) != 0
        or int(counts.get("pending_chunks", -1)) != 0
    ):
        raise ArtifactValidationError("embedding artifact 必须全部成功且无 pending/failed chunk")

    dimensions = int(embedding.get("dimensions", -1))
    if dimensions != expected_dimensions:
        raise ArtifactValidationError(
            f"embedding 维度不匹配：expected={expected_dimensions}, actual={dimensions}"
        )
    model = _require_text(embedding, "model", label="embedding manifest.embedding")
    if model != expected_model:
        raise ArtifactValidationError(
            f"embedding 模型不匹配：expected={expected_model}, actual={model}"
        )

    chunk_files = chunk_manifest.get("files")
    embedding_files = embedding_manifest.get("files")
    if not isinstance(chunk_files, dict) or not isinstance(embedding_files, dict):
        raise ArtifactValidationError("manifest.files 必须是 object")
    chunk_file_meta = chunk_files.get("chunks.jsonl")
    embedding_file_meta = embedding_files.get("embeddings.jsonl")
    if not isinstance(chunk_file_meta, dict) or not isinstance(embedding_file_meta, dict):
        raise ArtifactValidationError("manifest.files 缺少 chunks.jsonl 或 embeddings.jsonl")

    chunks_sha256 = _verify_file(
        chunk_root / "chunks.jsonl",
        chunk_file_meta.get("sha256"),
        label="chunks.jsonl",
    )
    if chunks_sha256 != str(source.get("chunks_sha256")):
        raise ArtifactValidationError("embedding source.chunks_sha256 与 chunks.jsonl 不一致")
    chunk_manifest_sha256 = _verify_file(
        chunk_manifest_path,
        source.get("chunk_manifest_sha256"),
        label="chunk manifest",
    )
    embeddings_sha256 = _verify_file(
        embedding_root / "embeddings.jsonl",
        embedding_file_meta.get("sha256"),
        label="embeddings.jsonl",
    )
    for filename in ("failures.jsonl", "profile.json", "usage.json"):
        metadata = embedding_files.get(filename)
        if not isinstance(metadata, dict):
            raise ArtifactValidationError(f"embedding manifest.files 缺少 {filename}")
        _verify_file(embedding_root / filename, metadata.get("sha256"), label=filename)

    return ArtifactBundle(
        chunk_root=chunk_root,
        embedding_root=embedding_root,
        chunk_manifest=chunk_manifest,
        embedding_manifest=embedding_manifest,
        chunk_build_id=chunk_build_id,
        embedding_artifact_id=artifact_id,
        embedding_profile_id=profile_id,
        embedding_model=model,
        embedding_dimensions=dimensions,
        expected_chunk_count=expected_count,
        chunk_manifest_sha256=chunk_manifest_sha256,
        artifact_manifest_sha256=_sha256(embedding_manifest_path),
        chunks_sha256=chunks_sha256,
        embeddings_sha256=embeddings_sha256,
    )


def _parse_json_line(raw: str, *, label: str, line_number: int) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactValidationError(f"{label} 第 {line_number} 行不是有效 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ArtifactValidationError(f"{label} 第 {line_number} 行必须是 JSON object")
    return payload


def _validate_vector(value: object, *, dimensions: int, chunk_id: str) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) != dimensions:
        raise ArtifactValidationError(f"Chunk {chunk_id} 的向量维度必须为 {dimensions}")
    try:
        vector = tuple(float(item) for item in value)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(f"Chunk {chunk_id} 的向量包含非数字值") from exc
    if not all(math.isfinite(item) for item in vector):
        raise ArtifactValidationError(f"Chunk {chunk_id} 的向量必须全部为有限值")
    if not any(item != 0.0 for item in vector):
        raise ArtifactValidationError(f"Chunk {chunk_id} 的向量不能全零")
    return vector


def iter_artifact_rows(bundle: ArtifactBundle) -> Iterator[ArtifactRow]:
    """按行流式关联 chunk/embedding；任何错位、重复或无效向量都会立即失败。"""
    chunks_path = bundle.chunk_root / "chunks.jsonl"
    embeddings_path = bundle.embedding_root / "embeddings.jsonl"
    seen_chunk_ids: set[str] = set()
    row_count = 0
    with (
        chunks_path.open(encoding="utf-8") as chunks_handle,
        embeddings_path.open(encoding="utf-8") as embeddings_handle,
    ):
        pairs = zip_longest(chunks_handle, embeddings_handle)
        for line_number, pair in enumerate(pairs, start=1):
            chunk_raw, embedding_raw = pair
            if chunk_raw is None or embedding_raw is None:
                raise ArtifactValidationError("chunks.jsonl 与 embeddings.jsonl 行数不一致")
            chunk = _parse_json_line(chunk_raw, label="chunks.jsonl", line_number=line_number)
            embedded = _parse_json_line(
                embedding_raw,
                label="embeddings.jsonl",
                line_number=line_number,
            )
            chunk_id = _require_text(chunk, "chunk_id", label=f"chunks.jsonl[{line_number}]")
            if chunk_id in seen_chunk_ids:
                raise ArtifactValidationError(f"重复 chunk_id：{chunk_id}")
            seen_chunk_ids.add(chunk_id)
            for key in ("chunk_id", "chunk_index", "content_hash"):
                if embedded.get(key) != chunk.get(key):
                    raise ArtifactValidationError(f"Chunk {chunk_id} 的 {key} 关联不一致")
            if embedded.get("profile_id") != bundle.embedding_profile_id:
                raise ArtifactValidationError(f"Chunk {chunk_id} 的 profile_id 不一致")
            if embedded.get("model") != bundle.embedding_model:
                raise ArtifactValidationError(f"Chunk {chunk_id} 的 model 不一致")
            if int(embedded.get("dimensions", -1)) != bundle.embedding_dimensions:
                raise ArtifactValidationError(f"Chunk {chunk_id} 的向量维度声明不一致")

            source_refs = chunk.get("source_refs", [])
            chapter_path = chunk.get("chapter_path", [])
            if not isinstance(source_refs, list) or not all(
                isinstance(item, dict) for item in source_refs
            ):
                raise ArtifactValidationError(
                    f"Chunk {chunk_id} 的 source_refs 必须是 object array"
                )
            if not isinstance(chapter_path, list):
                raise ArtifactValidationError(f"Chunk {chunk_id} 的 chapter_path 必须是 array")
            vector = _validate_vector(
                embedded.get("vector"),
                dimensions=bundle.embedding_dimensions,
                chunk_id=chunk_id,
            )
            content_role = _require_text(chunk, "content_role", label=f"Chunk {chunk_id}")
            retrieval_weight = float(chunk["retrieval_weight"])
            expected_weight = 0.65 if content_role == "answer_key" else 1.0
            if not math.isclose(retrieval_weight, expected_weight, abs_tol=1e-9):
                raise ArtifactValidationError(
                    f"Chunk {chunk_id} 的 retrieval_weight 与 content_role 不匹配"
                )
            row_count += 1
            yield ArtifactRow(
                chunk_id=chunk_id,
                chunk_index=int(chunk["chunk_index"]),
                book_id=_require_text(chunk, "book_id", label=f"Chunk {chunk_id}"),
                book_name=_require_text(chunk, "book_name", label=f"Chunk {chunk_id}"),
                grade_level=_require_text(chunk, "grade_level", label=f"Chunk {chunk_id}"),
                section=_require_text(chunk, "section", label=f"Chunk {chunk_id}"),
                chapter_path=tuple(str(item) for item in chapter_path),
                content_role=content_role,
                retrieval_weight=retrieval_weight,
                content_text=_require_text(chunk, "content_text", label=f"Chunk {chunk_id}"),
                embedding_text=_require_text(chunk, "embedding_text", label=f"Chunk {chunk_id}"),
                token_count=int(chunk["token_count"]),
                tokenizer_id=_require_text(chunk, "tokenizer_id", label=f"Chunk {chunk_id}"),
                source_page_start=int(chunk["source_page_start"]),
                source_page_end=int(chunk["source_page_end"]),
                source_refs=tuple(source_refs),
                content_hash=_require_text(chunk, "content_hash", label=f"Chunk {chunk_id}"),
                source_hash=_require_text(chunk, "source_hash", label=f"Chunk {chunk_id}"),
                embedding_input_hash=_require_text(
                    embedded,
                    "embedding_input_hash",
                    label=f"Chunk {chunk_id}",
                ),
                embedding=vector,
            )
    if row_count != bundle.expected_chunk_count:
        raise ArtifactValidationError(
            f"artifact 实际记录数不匹配：expected={bundle.expected_chunk_count}, actual={row_count}"
        )
