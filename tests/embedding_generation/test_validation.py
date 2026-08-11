"""Embedding 数据校验测试：固定 1024 维并拒绝无效浮点值。"""

from __future__ import annotations

import math

import pytest

from scripts.embedding_generation.validation import (
    VectorValidationError,
    embedding_cache_key,
    embedding_input_hash,
    validate_vector,
)


def test_validate_vector_accepts_exact_nonzero_1024_dimensions() -> None:
    vector = [0.0] * 1024
    vector[17] = 0.25

    validated = validate_vector(vector, dimensions=1024)

    assert isinstance(validated, tuple)
    assert len(validated) == 1024
    assert validated[17] == 0.25


@pytest.mark.parametrize(
    ("vector", "message"),
    [
        ([0.1] * 1023, "维度"),
        ([0.0] * 1024, "全零"),
        ([math.nan] + [0.1] * 1023, "有限"),
        ([math.inf] + [0.1] * 1023, "有限"),
    ],
)
def test_validate_vector_rejects_invalid_values(vector: list[float], message: str) -> None:
    with pytest.raises(VectorValidationError, match=message):
        validate_vector(vector, dimensions=1024)


def test_embedding_hash_and_cache_key_cover_text_model_and_dimensions() -> None:
    text_hash = embedding_input_hash("书名：教材\n\n函数定义")
    same = embedding_cache_key(
        content_hash="content-a",
        input_hash=text_hash,
        model="text-embedding-v4",
        dimensions=1024,
    )

    assert len(text_hash) == 64
    assert same == embedding_cache_key(
        content_hash="content-a",
        input_hash=text_hash,
        model="text-embedding-v4",
        dimensions=1024,
    )
    assert same != embedding_cache_key(
        content_hash="content-b",
        input_hash=text_hash,
        model="text-embedding-v4",
        dimensions=1024,
    )
    assert same != embedding_cache_key(
        content_hash="content-a",
        input_hash=embedding_input_hash("不同文本"),
        model="text-embedding-v4",
        dimensions=1024,
    )
    assert same != embedding_cache_key(
        content_hash="content-a",
        input_hash=text_hash,
        model="text-embedding-v4",
        dimensions=512,
    )
