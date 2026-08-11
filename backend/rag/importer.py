"""RAG artifact 导入器：校验后的 corpus 以 loading/ready/active 状态安全切换。"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Connection, Engine, text

from backend.rag.artifact_loader import ArtifactBundle, ArtifactRow, iter_artifact_rows
from backend.rag.database import create_rag_engine
from backend.rag.lexical import build_search_text, extract_formula_terms
from backend.rag.settings import RAGSettings, get_rag_settings


class RAGImportError(RuntimeError):
    """RAG 导入失败；失败 corpus 会保留为 failed 供审计。"""


@dataclass(frozen=True, slots=True)
class ImportResult:
    """一次 artifact 导入的可审计结果。"""

    corpus_id: str
    run_id: str | None
    status: str
    expected_chunks: int
    loaded_chunks: int
    already_present: bool = False


def _vector_literal(vector: tuple[float, ...]) -> str:
    return "[" + ",".join(repr(value) for value in vector) + "]"


def prepare_chunk_parameters(row: ArtifactRow, *, corpus_id: str) -> dict[str, Any]:
    """把 loader 行转换为 SQL 参数，同时生成 FTS/公式索引字段。"""
    searchable = "\n".join((row.book_name, *row.chapter_path, row.content_text))
    return {
        "corpus_id": corpus_id,
        "chunk_id": row.chunk_id,
        "chunk_index": row.chunk_index,
        "book_id": row.book_id,
        "book_name": row.book_name,
        "grade_level": row.grade_level,
        "section": row.section,
        "chapter_path": list(row.chapter_path),
        "content_role": row.content_role,
        "retrieval_weight": row.retrieval_weight,
        "content_text": row.content_text,
        "embedding_text": row.embedding_text,
        "token_count": row.token_count,
        "tokenizer_id": row.tokenizer_id,
        "source_page_start": row.source_page_start,
        "source_page_end": row.source_page_end,
        "source_refs_json": json.dumps(row.source_refs, ensure_ascii=False, separators=(",", ":")),
        "content_hash": row.content_hash,
        "source_hash": row.source_hash,
        "embedding_input_hash": row.embedding_input_hash,
        "search_text": build_search_text(searchable),
        "formula_terms": list(extract_formula_terms(row.content_text)),
        "embedding_literal": _vector_literal(row.embedding),
    }


_INSERT_CHUNK_SQL = text(
    """
    INSERT INTO rag.chunks (
        corpus_id, chunk_id, chunk_index, book_id, book_name, grade_level, section,
        chapter_path, content_role, retrieval_weight, content_text, embedding_text,
        token_count, tokenizer_id, source_page_start, source_page_end, source_refs,
        content_hash, source_hash, embedding_input_hash, search_text, search_vector,
        formula_terms, embedding
    ) VALUES (
        :corpus_id, CAST(:chunk_id AS uuid), :chunk_index, :book_id, :book_name, :grade_level,
        :section, CAST(:chapter_path AS text[]), :content_role, :retrieval_weight,
        :content_text, :embedding_text, :token_count, :tokenizer_id, :source_page_start,
        :source_page_end, CAST(:source_refs_json AS jsonb), :content_hash, :source_hash,
        :embedding_input_hash, :search_text, to_tsvector('simple', :search_text),
        CAST(:formula_terms AS text[]), CAST(:embedding_literal AS vector)
    )
    """
)

_INSERT_BOOK_SQL = text(
    """
    INSERT INTO rag.books (
        corpus_id, book_id, book_name, grade_level, chunk_count,
        source_page_start, source_page_end, metadata
    ) VALUES (
        :corpus_id, :book_id, :book_name, :grade_level, :chunk_count,
        :source_page_start, :source_page_end, CAST(:metadata AS jsonb)
    )
    """
)


def _book_parameters(corpus_id: str, stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "corpus_id": corpus_id,
            "book_id": book_id,
            "book_name": values["book_name"],
            "grade_level": values["grade_level"],
            "chunk_count": values["chunk_count"],
            "source_page_start": values["source_page_start"],
            "source_page_end": values["source_page_end"],
            "metadata": "{}",
        }
        for book_id, values in sorted(stats.items())
    ]


def _get_existing(connection: Connection, bundle: ArtifactBundle) -> dict[str, Any] | None:
    row = (
        connection.execute(
            text(
                """
            SELECT corpus_id, status, expected_chunk_count, loaded_chunk_count
            FROM rag.corpus_versions
            WHERE chunk_build_id = :chunk_build_id
              AND embedding_profile_id = :embedding_profile_id
            """
            ),
            {
                "chunk_build_id": bundle.chunk_build_id,
                "embedding_profile_id": bundle.embedding_profile_id,
            },
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def _insert_corpus_and_run(
    connection: Connection,
    bundle: ArtifactBundle,
    settings: RAGSettings,
) -> tuple[str, str]:
    corpus_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    metadata = json.dumps(
        {
            "chunk_root": str(bundle.chunk_root),
            "embedding_root": str(bundle.embedding_root),
            "embedding_model": bundle.embedding_model,
            "embedding_dimensions": bundle.embedding_dimensions,
            "lexical_pipeline_version": settings.lexical_pipeline_version,
        },
        ensure_ascii=False,
    )
    connection.execute(
        text(
            """
            INSERT INTO rag.corpus_versions (
                corpus_id, chunk_build_id, embedding_artifact_id, embedding_profile_id,
                embedding_model, embedding_dimensions, distance_metric, lexical_pipeline_version,
                expected_chunk_count, artifact_manifest_sha256, chunk_manifest_sha256,
                chunks_sha256, embeddings_sha256, status, metadata
            ) VALUES (
                :corpus_id, :chunk_build_id, :embedding_artifact_id, :embedding_profile_id,
                :embedding_model, :embedding_dimensions, 'cosine', :lexical_pipeline_version,
                :expected_chunk_count, :artifact_manifest_sha256, :chunk_manifest_sha256,
                :chunks_sha256, :embeddings_sha256, 'loading', CAST(:metadata AS jsonb)
            )
            """
        ),
        {
            "corpus_id": corpus_id,
            "chunk_build_id": bundle.chunk_build_id,
            "embedding_artifact_id": bundle.embedding_artifact_id,
            "embedding_profile_id": bundle.embedding_profile_id,
            "embedding_model": bundle.embedding_model,
            "embedding_dimensions": bundle.embedding_dimensions,
            "lexical_pipeline_version": settings.lexical_pipeline_version,
            "expected_chunk_count": bundle.expected_chunk_count,
            "artifact_manifest_sha256": bundle.artifact_manifest_sha256,
            "chunk_manifest_sha256": bundle.chunk_manifest_sha256,
            "chunks_sha256": bundle.chunks_sha256,
            "embeddings_sha256": bundle.embeddings_sha256,
            "metadata": metadata,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO rag.ingest_runs (
                run_id, corpus_id, status, expected_chunks, artifact_manifest_sha256,
                chunks_sha256, embeddings_sha256
            ) VALUES (
                :run_id, :corpus_id, 'running', :expected_chunks, :artifact_manifest_sha256,
                :chunks_sha256, :embeddings_sha256
            )
            """
        ),
        {
            "run_id": run_id,
            "corpus_id": corpus_id,
            "expected_chunks": bundle.expected_chunk_count,
            "artifact_manifest_sha256": bundle.artifact_manifest_sha256,
            "chunks_sha256": bundle.chunks_sha256,
            "embeddings_sha256": bundle.embeddings_sha256,
        },
    )
    return corpus_id, run_id


def _reset_existing(connection: Connection, existing: dict[str, Any]) -> tuple[str, str]:
    corpus_id = str(existing["corpus_id"])
    connection.execute(
        text("DELETE FROM rag.chunks WHERE corpus_id = :corpus_id"),
        {"corpus_id": corpus_id},
    )
    connection.execute(
        text("DELETE FROM rag.books WHERE corpus_id = :corpus_id"),
        {"corpus_id": corpus_id},
    )
    run_id = str(uuid.uuid4())
    connection.execute(
        text(
            """
            INSERT INTO rag.ingest_runs (
                run_id, corpus_id, status, expected_chunks, artifact_manifest_sha256,
                chunks_sha256, embeddings_sha256
            )
            SELECT :run_id, corpus_id, 'running', expected_chunk_count,
                   artifact_manifest_sha256, chunks_sha256, embeddings_sha256
            FROM rag.corpus_versions WHERE corpus_id = :corpus_id
            """
        ),
        {"run_id": run_id, "corpus_id": corpus_id},
    )
    connection.execute(
        text(
            """
            UPDATE rag.corpus_versions
            SET status = 'loading', loaded_chunk_count = 0, updated_at = now(), activated_at = NULL
            WHERE corpus_id = :corpus_id
            """
        ),
        {"corpus_id": corpus_id},
    )
    return corpus_id, run_id


def _activate(connection: Connection, corpus_id: str) -> None:
    connection.execute(
        text(
            "UPDATE rag.corpus_versions SET status = 'retired', updated_at = now() "
            "WHERE status = 'active'"
        )
    )
    connection.execute(
        text(
            """
            UPDATE rag.corpus_versions
            SET status = 'active', activated_at = now(), updated_at = now()
            WHERE corpus_id = :corpus_id
            """
        ),
        {"corpus_id": corpus_id},
    )


def import_artifacts(
    bundle: ArtifactBundle,
    *,
    settings: RAGSettings | None = None,
    engine: Engine | None = None,
    activate: bool = True,
) -> ImportResult:
    """把 artifact 导入 RAG corpus；重复导入同一版本安全幂等。"""
    config = settings or get_rag_settings()
    owned_engine = engine is None
    db = engine or create_rag_engine(config)
    connection = db.connect()
    corpus_id = ""
    run_id: str | None = None
    try:
        connection.execute(text("SELECT pg_advisory_lock(hashtext('rag:corpus-import'))"))
        connection.commit()
        existing: dict[str, Any] | None
        try:
            with connection.begin():
                existing = _get_existing(connection, bundle)
                if existing is not None and existing["status"] in {
                    "ready",
                    "active",
                    "retired",
                }:
                    corpus_id = str(existing["corpus_id"])
                    if activate and existing["status"] != "active":
                        _activate(connection, corpus_id)
                    return ImportResult(
                        corpus_id=corpus_id,
                        run_id=None,
                        status="active" if activate else str(existing["status"]),
                        expected_chunks=int(existing["expected_chunk_count"]),
                        loaded_chunks=int(existing["loaded_chunk_count"]),
                        already_present=True,
                    )
                if existing is None:
                    corpus_id, run_id = _insert_corpus_and_run(connection, bundle, config)
                else:
                    corpus_id, run_id = _reset_existing(connection, existing)
        except RAGImportError:
            raise
        except Exception as exc:
            raise RAGImportError(str(exc)) from exc

        book_stats: dict[str, dict[str, Any]] = defaultdict(dict)
        loaded_count = 0
        try:
            with connection.begin():
                batch: list[dict[str, Any]] = []
                for row in iter_artifact_rows(bundle):
                    params = prepare_chunk_parameters(row, corpus_id=corpus_id)
                    batch.append(params)
                    stats = book_stats.setdefault(
                        row.book_id,
                        {
                            "book_name": row.book_name,
                            "grade_level": row.grade_level,
                            "chunk_count": 0,
                            "source_page_start": row.source_page_start,
                            "source_page_end": row.source_page_end,
                        },
                    )
                    stats["chunk_count"] += 1
                    stats["source_page_start"] = min(
                        stats["source_page_start"], row.source_page_start
                    )
                    stats["source_page_end"] = max(stats["source_page_end"], row.source_page_end)
                    if len(batch) >= config.import_batch_size:
                        connection.execute(_INSERT_CHUNK_SQL, batch)
                        loaded_count += len(batch)
                        batch.clear()
                if batch:
                    connection.execute(_INSERT_CHUNK_SQL, batch)
                    loaded_count += len(batch)
                if loaded_count != bundle.expected_chunk_count:
                    raise RAGImportError(
                        "导入条数不匹配："
                        f"expected={bundle.expected_chunk_count}, actual={loaded_count}"
                    )
                connection.execute(_INSERT_BOOK_SQL, _book_parameters(corpus_id, book_stats))
                connection.execute(
                    text(
                        """
                        UPDATE rag.corpus_versions
                        SET loaded_chunk_count = :loaded_count, status = 'ready', updated_at = now()
                        WHERE corpus_id = :corpus_id
                        """
                    ),
                    {"corpus_id": corpus_id, "loaded_count": loaded_count},
                )
                connection.execute(
                    text(
                        """
                        UPDATE rag.ingest_runs
                        SET status = 'succeeded', loaded_chunks = :loaded_count, finished_at = now()
                        WHERE run_id = :run_id
                        """
                    ),
                    {"run_id": run_id, "loaded_count": loaded_count},
                )
        except Exception as exc:
            connection.rollback()
            if corpus_id and run_id:
                with connection.begin():
                    connection.execute(
                        text(
                            """
                            UPDATE rag.ingest_runs
                            SET status = 'failed', loaded_chunks = 0,
                                error_detail = :error_detail, finished_at = now()
                            WHERE run_id = :run_id
                            """
                        ),
                        {"run_id": run_id, "error_detail": str(exc)[:4000]},
                    )
                    connection.execute(
                        text(
                            """
                            UPDATE rag.corpus_versions
                            SET status = 'failed', loaded_chunk_count = 0, updated_at = now()
                            WHERE corpus_id = :corpus_id
                            """
                        ),
                        {"corpus_id": corpus_id},
                    )
            raise RAGImportError(str(exc)) from exc

        if activate:
            try:
                with connection.begin():
                    _activate(connection, corpus_id)
            except Exception as exc:
                raise RAGImportError(
                    f"Corpus {corpus_id} 已成功加载为 ready，但激活失败：{exc}"
                ) from exc
        return ImportResult(
            corpus_id=corpus_id,
            run_id=run_id,
            status="active" if activate else "ready",
            expected_chunks=bundle.expected_chunk_count,
            loaded_chunks=loaded_count,
        )
    finally:
        try:
            connection.execute(text("SELECT pg_advisory_unlock(hashtext('rag:corpus-import'))"))
            connection.commit()
        finally:
            connection.close()
            if owned_engine:
                db.dispose()
