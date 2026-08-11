"""OpenAI-compatible Embedding 客户端测试：覆盖请求参数、排序和错误分类。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import openai
import pytest

from scripts.embedding_generation.client import (
    EmbeddingRequestError,
    OpenAIEmbeddingClient,
)
from scripts.embedding_generation.settings import EmbeddingSettings


def _settings() -> EmbeddingSettings:
    return EmbeddingSettings(
        base_url="https://example.invalid/v1",
        api_key="secret-token",
        dimensions=3,
    )


def test_client_sends_explicit_model_dimensions_and_sorts_response_indexes() -> None:
    sdk = MagicMock()
    sdk.embeddings.create.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(index=1, embedding=[0.0, 0.2, 0.0]),
            SimpleNamespace(index=0, embedding=[0.1, 0.0, 0.0]),
        ],
        usage=SimpleNamespace(prompt_tokens=11, total_tokens=11),
    )
    client = OpenAIEmbeddingClient(_settings(), sdk_client=sdk)

    response = client.embed(["first", "second"])

    sdk.embeddings.create.assert_called_once_with(
        input=["first", "second"],
        model="text-embedding-v4",
        dimensions=3,
        encoding_format="float",
    )
    assert response.vectors == ((0.1, 0.0, 0.0), (0.0, 0.2, 0.0))
    assert response.usage.prompt_tokens == 11
    assert response.usage.total_tokens == 11


@pytest.mark.parametrize(
    ("exception", "expected_code", "retryable"),
    [
        (
            openai.APITimeoutError(request=httpx.Request("POST", "https://example.invalid")),
            "timeout",
            True,
        ),
        (
            openai.APIConnectionError(request=httpx.Request("POST", "https://example.invalid")),
            "connection",
            True,
        ),
        (
            openai.AuthenticationError(
                "bad key",
                response=httpx.Response(
                    401,
                    request=httpx.Request("POST", "https://example.invalid"),
                ),
                body=None,
            ),
            "http_401",
            False,
        ),
        (
            openai.BadRequestError(
                "bad input",
                response=httpx.Response(
                    400,
                    request=httpx.Request("POST", "https://example.invalid"),
                ),
                body=None,
            ),
            "http_400",
            False,
        ),
        (
            openai.InternalServerError(
                "server error",
                response=httpx.Response(
                    503,
                    request=httpx.Request("POST", "https://example.invalid"),
                ),
                body=None,
            ),
            "http_503",
            True,
        ),
    ],
)
def test_client_classifies_openai_errors(
    exception: Exception,
    expected_code: str,
    retryable: bool,
) -> None:
    sdk = MagicMock()
    sdk.embeddings.create.side_effect = exception
    client = OpenAIEmbeddingClient(_settings(), sdk_client=sdk)

    with pytest.raises(EmbeddingRequestError) as captured:
        client.embed(["text"])

    assert captured.value.code == expected_code
    assert captured.value.retryable is retryable
    assert "secret-token" not in str(captured.value)


def test_client_reads_retry_after_header() -> None:
    response = httpx.Response(
        429,
        headers={"Retry-After": "7.5"},
        request=httpx.Request("POST", "https://example.invalid"),
    )
    sdk = MagicMock()
    sdk.embeddings.create.side_effect = openai.RateLimitError(
        "slow down",
        response=response,
        body=None,
    )
    client = OpenAIEmbeddingClient(_settings(), sdk_client=sdk)

    with pytest.raises(EmbeddingRequestError) as captured:
        client.embed(["text"])

    assert captured.value.code == "http_429"
    assert captured.value.retryable is True
    assert captured.value.retry_after == 7.5


def test_client_rejects_missing_or_duplicate_response_indexes() -> None:
    sdk = MagicMock()
    sdk.embeddings.create.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(index=0, embedding=[0.1, 0.0, 0.0]),
            SimpleNamespace(index=0, embedding=[0.0, 0.2, 0.0]),
        ],
        usage=None,
    )
    client = OpenAIEmbeddingClient(_settings(), sdk_client=sdk)

    with pytest.raises(EmbeddingRequestError, match="index") as captured:
        client.embed(["first", "second"])

    assert captured.value.code == "invalid_response"
    assert captured.value.retryable is True


def test_client_rejects_invalid_vector_without_losing_input_position() -> None:
    sdk = MagicMock()
    sdk.embeddings.create.return_value = SimpleNamespace(
        data=[SimpleNamespace(index=0, embedding=[0.0, 0.0, 0.0])],
        usage=SimpleNamespace(prompt_tokens=2, total_tokens=2),
    )
    client = OpenAIEmbeddingClient(_settings(), sdk_client=sdk)

    with pytest.raises(EmbeddingRequestError, match="全零") as captured:
        client.embed(["text"])

    assert captured.value.code == "invalid_vector"
    assert captured.value.retryable is False


def test_client_does_not_swallow_process_interrupts() -> None:
    sdk = MagicMock()
    sdk.embeddings.create.side_effect = KeyboardInterrupt()
    client = OpenAIEmbeddingClient(_settings(), sdk_client=sdk)

    with pytest.raises(KeyboardInterrupt):
        client.embed(["text"])
