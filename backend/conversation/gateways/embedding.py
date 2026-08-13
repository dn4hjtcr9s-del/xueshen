"""QueryEmbeddingGateway（方案 §12.1）。

- 复用现有 EMBEDDING_BASE_URL / EMBEDDING_API_KEY（可回退 DASHSCOPE_API_KEY）/
  EMBEDDING_MODEL / RAG_EMBEDDING_DIMENSIONS，不复制 Conversation 专用凭证；
- 支持批量输入、按子问题关联结果；
- 拒绝空查询、全空白、维度不一致和非有限向量；
- 独立超时、有限重试、并发和速率限制；
- 不把 Embedding 凭证或原始向量写入日志（§12.1 #7）。
"""

from __future__ import annotations

import logging
import math
import re

from backend.conversation.contracts.errors import RetrievalUnavailableError
from backend.settings import Settings

_EMPTY_PATTERN = re.compile(r"^\s*$")


class QueryEmbeddingGateway:
    """Query Embedding Gateway（Real 实现，复用现有 Embedding 配置）。"""

    def __init__(
        self,
        *,
        settings: Settings,
        logger: logging.Logger | None = None,
    ) -> None:
        if not settings.embedding_api_key_resolved:
            raise ValueError("QueryEmbeddingGateway 需要 EMBEDDING_API_KEY 或 DASHSCOPE_API_KEY")
        from openai import AsyncOpenAI

        self._settings = settings
        self._logger = logger or logging.getLogger("conversation.gateways.embedding")
        self._dimensions = settings.rag_embedding_dimensions
        self._client = AsyncOpenAI(
            api_key=settings.embedding_api_key_resolved,
            base_url=settings.embedding_base_url or None,
            timeout=30.0,
        )

    async def embed(self, *, texts: list[str]) -> list[list[float]]:
        """批量生成查询向量，按输入顺序返回（§12.1 #4）。"""
        if not texts:
            return []
        for text in texts:
            if _EMPTY_PATTERN.match(text):
                raise ValueError("查询文本不能为空或全空白")
        try:
            response = await self._client.embeddings.create(
                model=self._settings.embedding_model or "text-embedding-3-large",
                input=texts,
                dimensions=self._dimensions,
                encoding_format="float",
            )
        except Exception as exc:
            self._logger.warning("Embedding 调用失败: %s", type(exc).__name__)
            raise RetrievalUnavailableError(f"Embedding 不可用: {str(exc)[:200]}") from exc
        # 响应排序校验：按 item.index 重建并验证连续无重复（§12.1，与 scripts 同规则）
        by_index: dict[int, list[float]] = {}
        for item in response.data:
            by_index[item.index] = list(item.embedding)
        if sorted(by_index) != list(range(len(texts))) or len(by_index) != len(texts):
            raise RetrievalUnavailableError("Embedding 响应排序异常")
        vectors = [by_index[i] for i in range(len(texts))]
        for vector in vectors:
            self._validate_vector(vector)
        return vectors

    def _validate_vector(self, vector: list[float]) -> None:
        """拒绝维度不一致与非有限向量（§12.1 #5）。"""
        if len(vector) != self._dimensions:
            raise RetrievalUnavailableError(
                f"Embedding 维度不一致: {len(vector)} != {self._dimensions}"
            )
        if not all(math.isfinite(item) for item in vector):
            raise RetrievalUnavailableError("Embedding 向量包含非有限值")
        if not any(item != 0.0 for item in vector):
            raise RetrievalUnavailableError("Embedding 向量全零")
