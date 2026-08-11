"""OpenAI-compatible Embedding 客户端：统一请求参数、响应排序与错误分类。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol, cast

import openai
from openai import OpenAI

from scripts.embedding_generation.schemas import ClientBatchResponse, UsageStats
from scripts.embedding_generation.settings import EmbeddingSettings
from scripts.embedding_generation.validation import VectorValidationError, validate_vector


class EmbeddingRequestError(RuntimeError):
    """携带稳定错误代码和可重试语义的脱敏 API 异常。"""

    def __init__(
        self,
        *,
        code: str,
        retryable: bool,
        message: str,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.code: str = code
        self.retryable: bool = retryable
        self.retry_after: float | None = retry_after


class EmbeddingClient(Protocol):
    """Runner 可注入的最小 embedding 客户端协议。"""

    def embed(self, texts: Sequence[str]) -> ClientBatchResponse:
        """按输入顺序返回同等数量的向量。"""
        ...


class _EmbeddingResource(Protocol):
    def create(self, **kwargs: Any) -> Any:
        """调用兼容 OpenAI 的 embeddings.create。"""
        ...


class _SDKClient(Protocol):
    embeddings: _EmbeddingResource


def _retry_after(error: openai.APIStatusError) -> float | None:
    raw_value = error.response.headers.get("Retry-After")
    if raw_value is None:
        return None
    try:
        value = float(raw_value)
    except ValueError:
        return None
    return value if value >= 0 else None


def _usage_value(usage: Any, name: str) -> int:
    if usage is None:
        return 0
    value = usage.get(name, 0) if isinstance(usage, dict) else getattr(usage, name, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class OpenAIEmbeddingClient:
    """调用供应商兼容端点，并关闭 SDK 内部重试以统一统计。"""

    def __init__(
        self,
        settings: EmbeddingSettings,
        *,
        sdk_client: _SDKClient | None = None,
    ) -> None:
        self._settings = settings
        self._sdk: _SDKClient = sdk_client or cast(
            _SDKClient,
            OpenAI(
                api_key=settings.api_key,
                base_url=settings.base_url,
                timeout=settings.timeout_seconds,
                max_retries=0,
            ),
        )

    def _safe_message(self, error: BaseException) -> str:
        message = str(error).replace(self._settings.api_key, "<redacted>")
        return message[:1000]

    def _raise_classified(self, error: Exception) -> None:
        if isinstance(error, openai.APITimeoutError):
            raise EmbeddingRequestError(
                code="timeout",
                retryable=True,
                message=self._safe_message(error),
            ) from error
        if isinstance(error, openai.APIConnectionError):
            raise EmbeddingRequestError(
                code="connection",
                retryable=True,
                message=self._safe_message(error),
            ) from error
        if isinstance(error, openai.APIStatusError):
            status = error.status_code
            retryable = status in {408, 409, 425, 429} or status >= 500
            raise EmbeddingRequestError(
                code=f"http_{status}",
                retryable=retryable,
                retry_after=_retry_after(error),
                message=self._safe_message(error),
            ) from error
        raise EmbeddingRequestError(
            code="client_error",
            retryable=False,
            message=self._safe_message(error),
        ) from error

    def embed(self, texts: Sequence[str]) -> ClientBatchResponse:
        """显式请求 float、模型和维度，并验证响应 index 与向量。"""
        if not texts:
            raise ValueError("embedding 请求不能为空")
        try:
            response = self._sdk.embeddings.create(
                input=list(texts),
                model=self._settings.model,
                dimensions=self._settings.dimensions,
                encoding_format="float",
            )
        except EmbeddingRequestError:
            raise
        except Exception as exc:
            self._raise_classified(exc)
            raise AssertionError("unreachable") from exc

        data = list(getattr(response, "data", []))
        indexes: list[int] = []
        by_index: dict[int, Any] = {}
        for item in data:
            try:
                index = int(item.index)
            except (TypeError, ValueError) as exc:
                raise EmbeddingRequestError(
                    code="invalid_response",
                    retryable=True,
                    message="Embedding 响应包含无效 index",
                ) from exc
            indexes.append(index)
            by_index[index] = item
        expected = list(range(len(texts)))
        if sorted(indexes) != expected or len(by_index) != len(texts):
            raise EmbeddingRequestError(
                code="invalid_response",
                retryable=True,
                message=f"Embedding 响应 index 异常：期望 {expected}，实际 {indexes}",
            )

        vectors: list[tuple[float, ...]] = []
        for index in expected:
            try:
                vector = validate_vector(
                    by_index[index].embedding,
                    dimensions=self._settings.dimensions,
                )
            except (AttributeError, TypeError, VectorValidationError) as exc:
                raise EmbeddingRequestError(
                    code="invalid_vector",
                    retryable=False,
                    message=f"Embedding 响应第 {index} 条向量无效：{exc}",
                ) from exc
            vectors.append(vector)

        usage = getattr(response, "usage", None)
        return ClientBatchResponse(
            vectors=tuple(vectors),
            usage=UsageStats(
                prompt_tokens=_usage_value(usage, "prompt_tokens"),
                total_tokens=_usage_value(usage, "total_tokens"),
            ),
        )
