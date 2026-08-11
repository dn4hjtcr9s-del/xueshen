"""阶段三本地验收 CLI：检查 schema、导入完整性、Recall、FTS、公式、过滤和引用。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean
from typing import Any

# 允许从任意工作目录以文件路径直接运行该 CLI。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import Engine, text

from backend.rag.database import create_rag_engine
from backend.rag.evaluation import recall_at_k
from backend.rag.retrieval import RetrievalService
from backend.rag.schemas import SearchFilters
from backend.rag.settings import get_rag_settings


@dataclass(frozen=True, slots=True)
class RecallSummary:
    """一个 K 值的 ANN Recall 汇总。"""

    k: int
    mean_recall: float
    minimum_recall: float
    target: float
    passed: bool


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample-count", type=int, default=12)
    parser.add_argument("--ks", type=int, nargs="+", default=[10, 20, 50])
    parser.add_argument(
        "--fts-query",
        action="append",
        dest="fts_queries",
        default=None,
        help="FTS 验收词；可重复传入",
    )
    return parser


def _database_stats(engine: Engine) -> dict[str, Any]:
    with engine.connect() as connection:
        corpus = (
            connection.execute(
                text(
                    """
                SELECT corpus_id::text, chunk_build_id, embedding_artifact_id,
                       embedding_profile_id, embedding_model, embedding_dimensions,
                       expected_chunk_count, loaded_chunk_count, status
                FROM rag.corpus_versions WHERE status = 'active'
                """
                )
            )
            .mappings()
            .one()
        )
        stats = (
            connection.execute(
                text(
                    """
                SELECT count(*) AS chunks,
                       count(DISTINCT book_id) AS books,
                       count(*) FILTER (WHERE vector_dims(embedding) <> 1024) AS bad_dimensions,
                       count(*) FILTER (WHERE source_refs = '[]'::jsonb) AS empty_source_refs,
                       count(*) FILTER (
                           WHERE source_page_start > source_page_end
                       ) AS bad_page_ranges,
                       count(*) FILTER (WHERE search_vector = ''::tsvector) AS empty_search_vectors
                FROM rag.chunks
                WHERE corpus_id = (
                    SELECT corpus_id FROM rag.corpus_versions WHERE status = 'active'
                )
                """
                )
            )
            .mappings()
            .one()
        )
        roles = (
            connection.execute(
                text(
                    """
                SELECT content_role, count(*) AS chunks,
                       min(retrieval_weight) AS min_weight,
                       max(retrieval_weight) AS max_weight
                FROM rag.chunks
                WHERE corpus_id = (
                    SELECT corpus_id FROM rag.corpus_versions WHERE status = 'active'
                )
                GROUP BY content_role ORDER BY content_role
                """
                )
            )
            .mappings()
            .all()
        )
        extensions = (
            connection.execute(
                text(
                    "SELECT extname FROM pg_extension "
                    "WHERE extname IN ('vector', 'pg_trgm') ORDER BY extname"
                )
            )
            .scalars()
            .all()
        )
        indexes = (
            connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE schemaname = 'rag' ORDER BY indexname")
            )
            .scalars()
            .all()
        )
        memory_tables = (
            connection.execute(
                text(
                    """
                SELECT table_schema || '.' || table_name
                FROM information_schema.tables
                WHERE table_name IN (
                    'memory_documents', 'memory_operations', 'memory_commits',
                    'memory_index_entries', 'knowledge_graph_nodes'
                )
                ORDER BY 1
                """
                )
            )
            .scalars()
            .all()
        )
    return {
        "corpus": dict(corpus),
        "stats": dict(stats),
        "roles": [dict(role) for role in roles],
        "extensions": extensions,
        "indexes": indexes,
        "memory_tables_in_rag_database": memory_tables,
    }


def _sample_vectors(engine: Engine, sample_count: int) -> list[dict[str, Any]]:
    if sample_count <= 0:
        raise ValueError("sample-count 必须大于 0")
    with engine.connect() as connection:
        total = connection.execute(
            text(
                """
                SELECT count(*) FROM rag.chunks
                WHERE corpus_id = (
                    SELECT corpus_id FROM rag.corpus_versions WHERE status = 'active'
                )
                """
            )
        ).scalar_one()
        step = max(1, total // sample_count)
        rows = (
            connection.execute(
                text(
                    """
                SELECT chunk_id::text, chunk_index, book_id, embedding::text AS embedding
                FROM rag.chunks
                WHERE corpus_id = (
                    SELECT corpus_id FROM rag.corpus_versions WHERE status = 'active'
                )
                  AND chunk_index % :step = 0
                ORDER BY chunk_index
                LIMIT :sample_count
                """
                ),
                {"step": step, "sample_count": sample_count},
            )
            .mappings()
            .all()
        )
    return [
        {
            "chunk_id": str(row["chunk_id"]),
            "chunk_index": int(row["chunk_index"]),
            "book_id": str(row["book_id"]),
            "embedding": json.loads(row["embedding"]),
        }
        for row in rows
    ]


def _recall_report(
    service: RetrievalService,
    samples: Sequence[dict[str, Any]],
    ks: Sequence[int],
) -> list[RecallSummary]:
    if not ks or any(k <= 0 or k > 200 for k in ks):
        raise ValueError("ks 必须位于 1 到 200")
    max_k = max(ks)
    per_k: dict[int, list[float]] = {k: [] for k in ks}
    for sample in samples:
        exact = service.exact_vector_search(sample["embedding"], limit=max_k)
        approximate = service.hnsw_vector_search(sample["embedding"], limit=max_k)
        exact_ids = [hit.chunk_id for hit in exact]
        approximate_ids = [hit.chunk_id for hit in approximate]
        for k in ks:
            per_k[k].append(recall_at_k(exact_ids, approximate_ids, k=k))
    summaries: list[RecallSummary] = []
    for k in sorted(per_k):
        values = per_k[k]
        target = 0.95 if k <= 10 else 0.98 if k >= 50 else 0.95
        average = mean(values) if values else 0.0
        minimum = min(values) if values else 0.0
        summaries.append(
            RecallSummary(
                k=k,
                mean_recall=average,
                minimum_recall=minimum,
                target=target,
                passed=average >= target,
            )
        )
    return summaries


def _retrieval_smoke(
    engine: Engine,
    service: RetrievalService,
    samples: Sequence[dict[str, Any]],
    fts_queries: Sequence[str],
) -> dict[str, Any]:
    fts_results = {
        query: [hit.chunk_id for hit in service.fts_search(query, limit=5)] for query in fts_queries
    }
    with engine.connect() as connection:
        formula = connection.execute(
            text(
                """
                SELECT formula_terms[1]
                FROM rag.chunks
                WHERE corpus_id = (
                    SELECT corpus_id FROM rag.corpus_versions WHERE status = 'active'
                )
                  AND cardinality(formula_terms) > 0
                ORDER BY chunk_index LIMIT 1
                """
            )
        ).scalar_one_or_none()
    formula_hits = service.formula_search(f"${formula}$", limit=5) if formula else ()
    sample = samples[0]
    filtered = service.hnsw_vector_search(
        sample["embedding"],
        limit=10,
        filters=SearchFilters(book_ids=(sample["book_id"],)),
    )
    hybrid = service.hybrid_search(
        query_text=fts_queries[0],
        query_vector=sample["embedding"],
        limit=10,
    )
    return {
        "fts": {
            query: {"hit_count": len(ids), "chunk_ids": ids} for query, ids in fts_results.items()
        },
        "formula": {
            "sample": formula,
            "hit_count": len(formula_hits),
            "chunk_ids": [hit.chunk_id for hit in formula_hits],
        },
        "book_filter": {
            "book_id": sample["book_id"],
            "hit_count": len(filtered),
            "all_match": bool(filtered)
            and all(hit.book_id == sample["book_id"] for hit in filtered),
        },
        "citation": {
            "has_result": bool(hybrid),
            "has_source_refs": bool(hybrid and hybrid[0].source_refs),
            "page_start": hybrid[0].source_page_start if hybrid else None,
            "page_end": hybrid[0].source_page_end if hybrid else None,
            "matched_channels": list(hybrid[0].matched_channels) if hybrid else [],
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    """执行只读验收并输出 JSON 报告。"""
    args = _parser().parse_args(argv)
    fts_queries = args.fts_queries or ["一元二次方程", "矩阵", "一致收敛"]
    settings = get_rag_settings()
    engine = create_rag_engine(settings)
    service = RetrievalService(settings=settings, engine=engine)
    try:
        samples = _sample_vectors(engine, args.sample_count)
        report = {
            "database": _database_stats(engine),
            "sample_count": len(samples),
            "recall": [asdict(item) for item in _recall_report(service, samples, args.ks)],
            "retrieval": _retrieval_smoke(engine, service, samples, fts_queries),
        }
    finally:
        engine.dispose()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
