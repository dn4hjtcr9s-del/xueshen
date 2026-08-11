"""Embedding 生成流水线共享的数据模型和序列化边界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class ChunkInput:
    """从阶段一 Chunk Artifact 读取的最小 embedding 输入。"""

    chunk_id: str
    chunk_index: int
    content_hash: str
    embedding_text: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ChunkInput:
        """解析并验证单条 Chunk 的阶段二必需字段。"""
        chunk_id = str(payload.get("chunk_id", "")).strip()
        content_hash = str(payload.get("content_hash", "")).strip()
        embedding_text = str(payload.get("embedding_text", "")).strip()
        if not chunk_id:
            raise ValueError("chunk_id 不能为空")
        if not content_hash:
            raise ValueError(f"Chunk {chunk_id} 的 content_hash 不能为空")
        if not embedding_text:
            raise ValueError(f"Chunk {chunk_id} 的 embedding_text 不能为空")
        return cls(
            chunk_id=chunk_id,
            chunk_index=int(payload["chunk_index"]),
            content_hash=content_hash,
            embedding_text=embedding_text,
        )


@dataclass(frozen=True, slots=True)
class UsageStats:
    """供应商返回的 token 用量；缺失字段按零处理。"""

    prompt_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: UsageStats) -> UsageStats:
        return UsageStats(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class ClientBatchResponse:
    """已按输入顺序排列的单批 API 响应。"""

    vectors: tuple[tuple[float, ...], ...]
    usage: UsageStats


@dataclass(frozen=True, slots=True)
class EmbeddingJob:
    """一个唯一 embedding 输入及所有可复用该结果的 Chunk。"""

    cache_key: str
    input_hash: str
    content_hash: str
    embedding_text: str
    chunks: tuple[ChunkInput, ...]


@dataclass(frozen=True, slots=True)
class BatchPlan:
    """由稳定 cache key 列表定义的不可变批次。"""

    batch_index: int
    batch_id: str
    jobs: tuple[EmbeddingJob, ...]


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    """Shard 中单个 Chunk 的成功向量或永久失败记录。"""

    status: Literal["success", "failed"]
    chunk_id: str
    chunk_index: int
    content_hash: str
    embedding_input_hash: str
    cache_key: str
    profile_id: str
    model: str
    dimensions: int
    vector: tuple[float, ...] | None = None
    cached_from_chunk_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "record_type": "embedding_result",
            "status": self.status,
            "chunk_id": self.chunk_id,
            "chunk_index": self.chunk_index,
            "content_hash": self.content_hash,
            "embedding_input_hash": self.embedding_input_hash,
            "cache_key": self.cache_key,
            "profile_id": self.profile_id,
            "model": self.model,
            "dimensions": self.dimensions,
            "attempts": self.attempts,
        }
        if self.status == "success":
            payload["vector"] = list(self.vector or ())
            if self.cached_from_chunk_id is not None:
                payload["cached_from_chunk_id"] = self.cached_from_chunk_id
        else:
            payload["error_code"] = self.error_code
            payload["error_message"] = self.error_message
        return payload


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """一个完整稳定批次的可持久化执行结果。"""

    batch_index: int
    batch_id: str
    records: tuple[ArtifactRecord, ...]
    usage: UsageStats
    request_count: int
    retry_count: int
    api_input_count: int


@dataclass(frozen=True, slots=True)
class RunSummary:
    """一次 runner 调用的持久化结果和未完成批次摘要。"""

    manifest: dict[str, Any]
    completed_batches: int
    deferred_batches: tuple[int, ...]
