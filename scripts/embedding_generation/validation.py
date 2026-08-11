"""Embedding 输入哈希与向量质量门禁。"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable


class VectorValidationError(ValueError):
    """表示供应商返回的向量不能安全进入 Artifact。"""


def embedding_input_hash(text: str) -> str:
    """计算实际发送给 embedding API 的 UTF-8 文本哈希。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embedding_cache_key(
    *,
    content_hash: str,
    input_hash: str,
    model: str,
    dimensions: int,
) -> str:
    """生成跨 Chunk 去重所需的稳定缓存键。"""
    payload = {
        "content_hash": content_hash,
        "dimensions": dimensions,
        "embedding_input_hash": input_hash,
        "model": model,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_vector(vector: Iterable[float], *, dimensions: int) -> tuple[float, ...]:
    """验证向量维度、有限性和非全零约束，并转换成不可变 tuple。"""
    try:
        values = tuple(float(value) for value in vector)
    except (TypeError, ValueError) as exc:
        raise VectorValidationError("向量包含无法转换为 float 的元素") from exc
    if len(values) != dimensions:
        raise VectorValidationError(f"向量维度错误：期望 {dimensions}，实际 {len(values)}")
    if not all(math.isfinite(value) for value in values):
        raise VectorValidationError("向量必须全部是有限浮点数")
    if not any(value != 0.0 for value in values):
        raise VectorValidationError("向量不得为全零")
    return values
