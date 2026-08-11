"""创建 RAG 独立核心 schema：版本、教材、chunk、导入运行和检索索引。"""

from __future__ import annotations

from alembic import op

revision = "0001_rag_core"
down_revision = None
branch_labels = None
depends_on = None


_CONTENT_ROLES = (
    "body",
    "definition",
    "theorem",
    "proof",
    "example",
    "solution",
    "exercise",
    "answer_key",
    "formula",
    "table",
    "figure_caption",
    "appendix",
)


def upgrade() -> None:
    """创建所有 RAG 表；不创建或修改任何 Memory 表。"""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE SCHEMA IF NOT EXISTS rag")

    op.execute(
        """
        CREATE TABLE rag.corpus_versions (
            corpus_id uuid PRIMARY KEY,
            chunk_build_id varchar(128) NOT NULL,
            embedding_artifact_id varchar(128) NOT NULL,
            embedding_profile_id varchar(128) NOT NULL,
            embedding_model text NOT NULL,
            embedding_dimensions integer NOT NULL CHECK (embedding_dimensions = 1024),
            distance_metric text NOT NULL CHECK (distance_metric = 'cosine'),
            lexical_pipeline_version varchar(128) NOT NULL,
            expected_chunk_count integer NOT NULL CHECK (expected_chunk_count >= 0),
            loaded_chunk_count integer NOT NULL DEFAULT 0 CHECK (loaded_chunk_count >= 0),
            artifact_manifest_sha256 char(64) NOT NULL,
            chunk_manifest_sha256 char(64) NOT NULL,
            chunks_sha256 char(64) NOT NULL,
            embeddings_sha256 char(64) NOT NULL,
            status text NOT NULL
                CHECK (status IN ('loading', 'ready', 'active', 'retired', 'failed')),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            activated_at timestamptz,
            UNIQUE (chunk_build_id, embedding_profile_id)
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ix_rag_corpus_one_active
        ON rag.corpus_versions (status)
        WHERE status = 'active'
        """
    )

    op.execute(
        """
        CREATE TABLE rag.books (
            corpus_id uuid NOT NULL REFERENCES rag.corpus_versions(corpus_id) ON DELETE CASCADE,
            book_id text NOT NULL,
            book_name text NOT NULL,
            grade_level text NOT NULL,
            chunk_count integer NOT NULL DEFAULT 0 CHECK (chunk_count >= 0),
            source_page_start integer,
            source_page_end integer,
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            PRIMARY KEY (corpus_id, book_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_rag_books_grade ON rag.books (corpus_id, grade_level)")

    roles_sql = ", ".join(f"'{role}'" for role in _CONTENT_ROLES)
    op.execute(
        f"""
        CREATE TABLE rag.chunks (
            corpus_id uuid NOT NULL REFERENCES rag.corpus_versions(corpus_id) ON DELETE CASCADE,
            chunk_id uuid NOT NULL,
            chunk_index integer NOT NULL CHECK (chunk_index >= 0),
            book_id text NOT NULL,
            book_name text NOT NULL,
            grade_level text NOT NULL,
            section text NOT NULL,
            chapter_path text[] NOT NULL DEFAULT '{{}}',
            content_role text NOT NULL CHECK (content_role IN ({roles_sql})),
            retrieval_weight real NOT NULL CHECK (retrieval_weight > 0 AND retrieval_weight <= 1),
            content_text text NOT NULL,
            embedding_text text NOT NULL,
            token_count integer NOT NULL CHECK (token_count >= 0),
            tokenizer_id text NOT NULL,
            source_page_start integer NOT NULL,
            source_page_end integer NOT NULL,
            source_refs jsonb NOT NULL DEFAULT '[]'::jsonb
                CHECK (jsonb_typeof(source_refs) = 'array'),
            content_hash char(64) NOT NULL,
            source_hash char(64) NOT NULL,
            embedding_input_hash char(64) NOT NULL,
            search_text text NOT NULL,
            search_vector tsvector NOT NULL,
            formula_terms text[] NOT NULL DEFAULT '{{}}',
            embedding vector(1024) NOT NULL,
            PRIMARY KEY (corpus_id, chunk_id),
            UNIQUE (corpus_id, chunk_index),
            FOREIGN KEY (corpus_id, book_id)
                REFERENCES rag.books (corpus_id, book_id)
                DEFERRABLE INITIALLY DEFERRED
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_rag_chunks_hnsw_embedding ON rag.chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX ix_rag_chunks_fts ON rag.chunks USING gin (search_vector)")
    op.execute("CREATE INDEX ix_rag_chunks_formula ON rag.chunks USING gin (formula_terms)")
    op.execute(
        "CREATE INDEX ix_rag_chunks_book_page "
        "ON rag.chunks (corpus_id, book_id, source_page_start, source_page_end)"
    )
    op.execute("CREATE INDEX ix_rag_chunks_grade ON rag.chunks (corpus_id, grade_level)")
    op.execute("CREATE INDEX ix_rag_chunks_role ON rag.chunks (corpus_id, content_role)")
    op.execute("CREATE INDEX ix_rag_chunks_section ON rag.chunks (corpus_id, section)")
    op.execute("CREATE INDEX ix_rag_chunks_chapter_path ON rag.chunks (corpus_id, chapter_path)")

    op.execute(
        """
        CREATE TABLE rag.ingest_runs (
            run_id uuid PRIMARY KEY,
            corpus_id uuid NOT NULL REFERENCES rag.corpus_versions(corpus_id) ON DELETE CASCADE,
            status text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
            expected_chunks integer NOT NULL CHECK (expected_chunks >= 0),
            loaded_chunks integer NOT NULL DEFAULT 0 CHECK (loaded_chunks >= 0),
            rejected_chunks integer NOT NULL DEFAULT 0 CHECK (rejected_chunks >= 0),
            artifact_manifest_sha256 char(64) NOT NULL,
            chunks_sha256 char(64) NOT NULL,
            embeddings_sha256 char(64) NOT NULL,
            error_detail text,
            started_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_rag_ingest_runs_corpus ON rag.ingest_runs (corpus_id, started_at DESC)"
    )


def downgrade() -> None:
    """只删除 RAG schema，保持 downgrade 边界不越过 Memory 数据库。"""
    op.execute("DROP SCHEMA IF EXISTS rag CASCADE")
