"""Embedding 配置加载：隔离 secret，并实现 env/CLI 的确定性优先级。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Self


class SettingsError(ValueError):
    """表示 embedding 配置缺失或数值不合法。"""


_ENV_FIELDS: dict[str, str] = {
    "EMBEDDING_BASE_URL": "base_url",
    "EMBEDDING_MODEL": "model",
    "RAG_EMBEDDING_DIMENSIONS": "dimensions",
    "RAG_EMBEDDING_BATCH_SIZE": "batch_size",
    "RAG_EMBEDDING_CONCURRENCY": "concurrency",
    "RAG_EMBEDDING_TIMEOUT_SECONDS": "timeout_seconds",
    "RAG_EMBEDDING_MAX_ATTEMPTS": "max_attempts",
    "RAG_EMBEDDING_INITIAL_BACKOFF_SECONDS": "initial_backoff_seconds",
    "RAG_EMBEDDING_MAX_BACKOFF_SECONDS": "max_backoff_seconds",
    "RAG_EMBEDDING_JITTER_SECONDS": "jitter_seconds",
    "RAG_EMBEDDING_REQUESTS_PER_SECOND": "requests_per_second",
    "RAG_EMBEDDING_PRICE_PER_MILLION_TOKENS": "price_per_million_tokens",
}


def _read_env_file(path: Path | None) -> dict[str, str]:
    """读取显式 env 文件；不修改全局进程环境。"""
    if path is None:
        return {}
    if not path.is_file():
        raise SettingsError(f"env 文件不存在：{path}")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _positive_int(name: str, value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} 必须是正整数") from exc
    if parsed <= 0:
        raise SettingsError(f"{name} 必须是正整数")
    return parsed


def _nonnegative_float(name: str, value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} 必须是非负数") from exc
    if parsed < 0:
        raise SettingsError(f"{name} 必须是非负数")
    return parsed


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    """一次 embedding 运行的完整配置；API key 不参与 repr。"""

    base_url: str
    api_key: str = field(repr=False)
    model: str = "text-embedding-v4"
    dimensions: int = 1024
    batch_size: int = 10
    concurrency: int = 4
    timeout_seconds: float = 60.0
    max_attempts: int = 6
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    jitter_seconds: float = 0.5
    requests_per_second: float = 0.0
    price_per_million_tokens: Decimal | None = None

    @classmethod
    def from_sources(
        cls,
        *,
        env_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> Self:
        """按 CLI 覆盖值、进程环境、env 文件和默认值的顺序加载配置。"""
        file_values = _read_env_file(env_file)
        process_values = dict(os.environ if environ is None else environ)
        merged_env = {**file_values, **process_values}

        values: dict[str, Any] = {}
        for env_name, field_name in _ENV_FIELDS.items():
            if env_name in merged_env and merged_env[env_name] != "":
                values[field_name] = merged_env[env_name]

        embedding_key = merged_env.get("EMBEDDING_API_KEY", "").strip()
        dashscope_key = merged_env.get("DASHSCOPE_API_KEY", "").strip()
        if embedding_key or dashscope_key:
            values["api_key"] = embedding_key or dashscope_key
        if overrides:
            values.update({key: value for key, value in overrides.items() if value is not None})

        base_url = str(values.get("base_url", "")).strip()
        if not base_url:
            raise SettingsError("缺少 EMBEDDING_BASE_URL")
        api_key = str(values.get("api_key", "")).strip()
        if not api_key:
            raise SettingsError("缺少 Embedding API key（EMBEDDING_API_KEY 或 DASHSCOPE_API_KEY）")
        model = str(values.get("model", "text-embedding-v4")).strip()
        if not model:
            raise SettingsError("EMBEDDING_MODEL 不能为空")

        price_value = values.get("price_per_million_tokens")
        price: Decimal | None = None
        if price_value not in (None, ""):
            try:
                price = Decimal(str(price_value))
            except InvalidOperation as exc:
                raise SettingsError("price_per_million_tokens 必须是非负十进制数") from exc
            if price < 0:
                raise SettingsError("price_per_million_tokens 必须是非负十进制数")

        settings = cls(
            base_url=base_url,
            api_key=api_key,
            model=model,
            dimensions=_positive_int("dimensions", values.get("dimensions", 1024)),
            batch_size=_positive_int("batch_size", values.get("batch_size", 10)),
            concurrency=_positive_int("concurrency", values.get("concurrency", 4)),
            timeout_seconds=_nonnegative_float(
                "timeout_seconds", values.get("timeout_seconds", 60.0)
            ),
            max_attempts=_positive_int("max_attempts", values.get("max_attempts", 6)),
            initial_backoff_seconds=_nonnegative_float(
                "initial_backoff_seconds", values.get("initial_backoff_seconds", 1.0)
            ),
            max_backoff_seconds=_nonnegative_float(
                "max_backoff_seconds", values.get("max_backoff_seconds", 30.0)
            ),
            jitter_seconds=_nonnegative_float(
                "jitter_seconds", values.get("jitter_seconds", 0.5)
            ),
            requests_per_second=_nonnegative_float(
                "requests_per_second", values.get("requests_per_second", 0.0)
            ),
            price_per_million_tokens=price,
        )
        if settings.timeout_seconds == 0:
            raise SettingsError("timeout_seconds 必须大于零")
        if settings.max_backoff_seconds < settings.initial_backoff_seconds:
            raise SettingsError("max_backoff_seconds 不得小于 initial_backoff_seconds")
        return settings
