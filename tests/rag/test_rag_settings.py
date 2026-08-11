"""RAG 独立配置测试：禁止读取 Memory DATABASE_URL。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.rag.database import create_rag_engine
from backend.rag.settings import RAGSettings


def test_rag_settings_requires_independent_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAG_DATABASE_URL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://memory:memory@127.0.0.1:55432/memory",
    )

    with pytest.raises(ValidationError, match="RAG_DATABASE_URL"):
        RAGSettings(_env_file=None)


def test_rag_settings_parse_rag_only_values(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+psycopg://rag:rag@127.0.0.1:55433/rag"
    monkeypatch.setenv("RAG_DATABASE_URL", url)
    monkeypatch.setenv("RAG_HNSW_EF_SEARCH", "120")

    settings = RAGSettings(_env_file=None)
    engine = create_rag_engine(settings)

    assert settings.database_url == url
    assert settings.hnsw_ef_search == 120
    assert settings.embedding_dimensions == 1024
    assert engine.url.render_as_string(hide_password=False) == url
    engine.dispose()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RAG_DATABASE_SCHEMA", "public"),
        ("RAG_EMBEDDING_MODEL", "unexpected-model"),
        ("RAG_EMBEDDING_DIMENSIONS", "1536"),
        ("RAG_LEXICAL_PIPELINE_VERSION", "future/v2"),
    ],
)
def test_rag_settings_rejects_values_incompatible_with_fixed_schema(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    monkeypatch.setenv(
        "RAG_DATABASE_URL",
        "postgresql+psycopg://rag:rag@127.0.0.1:55433/rag",
    )
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        RAGSettings(_env_file=None)
