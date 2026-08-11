"""RAG 独立 migration 测试：验证 schema 和版本链不触碰 Memory。"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_rag_alembic_is_independent_from_memory() -> None:
    config = (_PROJECT_ROOT / "rag_alembic.ini").read_text(encoding="utf-8")
    env = (_PROJECT_ROOT / "rag_migrations" / "env.py").read_text(encoding="utf-8")

    assert "script_location = rag_migrations" in config
    assert "RAG_DATABASE_URL" in env
    assert 'os.environ.get("DATABASE_URL")' not in env
    assert "rag_alembic_version" in env
    assert "backend.settings" not in env
    assert "alembic/env.py" not in env


def test_rag_core_migration_declares_isolated_vector_fts_and_provenance_schema() -> None:
    migration = (_PROJECT_ROOT / "rag_migrations" / "versions" / "0001_rag_core.py").read_text(
        encoding="utf-8"
    )

    for required in (
        "CREATE EXTENSION IF NOT EXISTS vector",
        "CREATE EXTENSION IF NOT EXISTS pg_trgm",
        "CREATE SCHEMA IF NOT EXISTS rag",
        "rag.corpus_versions",
        "rag.books",
        "rag.chunks",
        "rag.ingest_runs",
        "vector(1024)",
        "search_vector",
        "formula_terms",
        "source_refs",
        "vector_cosine_ops",
    ):
        assert required in migration

    assert "memory_documents" not in migration
    assert "DATABASE_URL" not in migration
