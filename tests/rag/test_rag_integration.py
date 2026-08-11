"""真实 pgvector 集成测试：通过 RAG_TEST_DATABASE_URL 显式启用，只读验收本地 RAG 库。"""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import Engine, create_engine, text

from backend.rag.retrieval import RetrievalService
from backend.rag.schemas import SearchFilters
from backend.rag.settings import RAGSettings

_TEST_URL = os.environ.get("RAG_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not _TEST_URL, reason="未设置 RAG_TEST_DATABASE_URL")


@pytest.fixture(scope="module")
def engine() -> Engine:
    database = create_engine(_TEST_URL, pool_pre_ping=True)
    yield database
    database.dispose()


@pytest.fixture(scope="module")
def service(engine: Engine) -> RetrievalService:
    settings = RAGSettings(RAG_DATABASE_URL=_TEST_URL, _env_file=None)
    return RetrievalService(settings=settings, engine=engine)


def test_schema_extensions_and_full_active_corpus(engine: Engine) -> None:
    with engine.connect() as connection:
        extensions = set(
            connection.execute(
                text("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')")
            ).scalars()
        )
        corpus = connection.execute(
            text(
                """
                SELECT expected_chunk_count, loaded_chunk_count, status
                FROM rag.corpus_versions WHERE status = 'active'
                """
            )
        ).one()
        chunk_count = connection.execute(text("SELECT count(*) FROM rag.chunks")).scalar_one()
        book_count = connection.execute(text("SELECT count(*) FROM rag.books")).scalar_one()
        invalid_dimensions = connection.execute(
            text("SELECT count(*) FROM rag.chunks WHERE vector_dims(embedding) <> 1024")
        ).scalar_one()

    assert extensions == {"vector", "pg_trgm"}
    assert tuple(corpus) == (15000, 15000, "active")
    assert chunk_count == 15000
    assert book_count == 21
    assert invalid_dimensions == 0


def test_exact_hnsw_fts_formula_and_provenance(engine: Engine, service: RetrievalService) -> None:
    with engine.connect() as connection:
        sample = connection.execute(
            text(
                """
                SELECT embedding::text, book_id
                FROM rag.chunks
                WHERE corpus_id = (
                    SELECT corpus_id FROM rag.corpus_versions WHERE status = 'active'
                )
                ORDER BY chunk_index
                LIMIT 1
                """
            )
        ).one()
        formula = connection.execute(
            text(
                """
                SELECT formula_terms[1]
                FROM rag.chunks
                WHERE cardinality(formula_terms) > 0
                ORDER BY chunk_index
                LIMIT 1
                """
            )
        ).scalar_one()

    vector = json.loads(sample[0])
    exact = service.exact_vector_search(vector, limit=10)
    hnsw = service.hnsw_vector_search(vector, limit=10)
    fts = service.fts_search("一元二次方程", limit=10)
    formula_hits = service.formula_search(f"${formula}$", limit=10)
    filtered = service.hnsw_vector_search(
        vector,
        limit=10,
        filters=SearchFilters(book_ids=(sample[1],)),
    )

    assert exact and hnsw
    assert exact[0].chunk_id == hnsw[0].chunk_id
    assert fts
    assert formula_hits
    assert filtered and all(hit.book_id == sample[1] for hit in filtered)
    assert exact[0].source_page_start <= exact[0].source_page_end
    assert exact[0].source_refs
