"""Embedding 配置测试：验证优先级、默认值和 secret 边界。"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.embedding_generation.settings import EmbeddingSettings, SettingsError


def test_settings_load_env_file_and_dashscope_key_fallback(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "EMBEDDING_BASE_URL=https://example.invalid/v1",
                "EMBEDDING_MODEL=text-embedding-v4",
                "DASHSCOPE_API_KEY='secret-from-file'",
            ]
        ),
        encoding="utf-8",
    )

    settings = EmbeddingSettings.from_sources(env_file=env_file, environ={})

    assert settings.base_url == "https://example.invalid/v1"
    assert settings.model == "text-embedding-v4"
    assert settings.api_key == "secret-from-file"
    assert settings.dimensions == 1024
    assert settings.batch_size == 10
    assert settings.concurrency == 4
    assert "secret-from-file" not in repr(settings)


def test_settings_precedence_is_override_then_environ_then_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "EMBEDDING_BASE_URL=https://file.invalid/v1",
                "EMBEDDING_API_KEY=file-secret",
                "RAG_EMBEDDING_BATCH_SIZE=8",
                "RAG_EMBEDDING_CONCURRENCY=2",
            ]
        ),
        encoding="utf-8",
    )

    settings = EmbeddingSettings.from_sources(
        env_file=env_file,
        environ={
            "EMBEDDING_BASE_URL": "https://env.invalid/v1",
            "EMBEDDING_API_KEY": "env-secret",
            "RAG_EMBEDDING_BATCH_SIZE": "9",
        },
        overrides={"batch_size": 7},
    )

    assert settings.base_url == "https://env.invalid/v1"
    assert settings.api_key == "env-secret"
    assert settings.batch_size == 7
    assert settings.concurrency == 2


def test_settings_reject_missing_required_api_configuration() -> None:
    with pytest.raises(SettingsError, match="EMBEDDING_BASE_URL"):
        EmbeddingSettings.from_sources(environ={"EMBEDDING_API_KEY": "secret"})

    with pytest.raises(SettingsError, match="API key"):
        EmbeddingSettings.from_sources(
            environ={"EMBEDDING_BASE_URL": "https://example.invalid/v1"}
        )


def test_settings_reject_invalid_positive_limits() -> None:
    base = {
        "EMBEDDING_BASE_URL": "https://example.invalid/v1",
        "EMBEDDING_API_KEY": "secret",
    }

    with pytest.raises(SettingsError, match="batch_size"):
        EmbeddingSettings.from_sources(environ=base, overrides={"batch_size": 0})
    with pytest.raises(SettingsError, match="dimensions"):
        EmbeddingSettings.from_sources(environ=base, overrides={"dimensions": 0})
    with pytest.raises(SettingsError, match="max_attempts"):
        EmbeddingSettings.from_sources(environ=base, overrides={"max_attempts": 0})
