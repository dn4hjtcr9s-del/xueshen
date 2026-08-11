"""RAG 检索服务：提供 exact/HNSW、FTS、公式和应用层 RRF 融合。"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from sqlalchemy import Engine, text

from backend.rag.database import create_rag_engine
from backend.rag.lexical import build_tsquery, extract_formula_terms
from backend.rag.rrf import RankedCandidate, fuse_ranked_results
from backend.rag.schemas import SearchFilters, SearchHit
from backend.rag.settings import RAGSettings, get_rag_settings

_SELECT_COLUMNS = """
    c.chunk_id::text AS chunk_id,
    c.retrieval_weight,
    c.book_id,
    c.book_name,
    c.grade_level,
    c.section,
    c.chapter_path,
    c.content_role,
    c.content_text,
    c.source_page_start,
    c.source_page_end,
    c.source_refs
"""


def serialize_query_vector(vector: Sequence[float], *, dimensions: int = 1024) -> str:
    """校验查询向量并转换为 pgvector 接受的文本格式。"""
    if len(vector) != dimensions:
        raise ValueError(f"查询向量维度必须为 {dimensions}")
    values = tuple(float(item) for item in vector)
    if not all(math.isfinite(item) for item in values):
        raise ValueError("查询向量必须全部为有限值")
    if not any(item != 0.0 for item in values):
        raise ValueError("查询向量不能全零")
    return "[" + ",".join(repr(item) for item in values) + "]"


def build_filter_clause(filters: SearchFilters | None) -> tuple[str, dict[str, Any]]:
    """生成参数化过滤 SQL；chapter_prefix 使用数组前缀而非包含关系。"""
    if filters is None:
        return "", {}
    predicates: list[str] = []
    params: dict[str, Any] = {}
    for field_name, column_name in (
        ("book_ids", "book_id"),
        ("grade_levels", "grade_level"),
        ("sections", "section"),
        ("content_roles", "content_role"),
    ):
        values = getattr(filters, field_name)
        if values:
            predicates.append(f"c.{column_name} = ANY(CAST(:{field_name} AS text[]))")
            params[field_name] = list(values)
    if filters.chapter_prefix:
        predicates.append(
            "c.chapter_path[1:cardinality(CAST(:chapter_prefix AS text[]))] "
            "= CAST(:chapter_prefix AS text[])"
        )
        params["chapter_prefix"] = list(filters.chapter_prefix)
    if not predicates:
        return "", params
    return "\n      AND " + "\n      AND ".join(predicates), params


def prepare_fts_query(query: str) -> str:
    """将用户文本转换为 simple 配置可执行的中文二元组 tsquery。"""
    tsquery = build_tsquery(query)
    if not tsquery:
        raise ValueError("FTS 查询没有可索引词元")
    return tsquery


def prepare_formula_query(query: str) -> tuple[str, ...]:
    """提取公式；无分隔符时允许直接输入一个包含数学运算符的公式。"""
    extracted = extract_formula_terms(query)
    if extracted:
        return extracted
    normalized = unicodedata.normalize("NFKC", query).lower().strip()
    if not normalized or re.search(r"[=+\-*/^_\\()]", normalized) is None:
        return ()
    canonical = re.sub(r"\s+", "", normalized).strip("$")
    return (canonical,) if canonical else ()


def _validate_limit(limit: int) -> int:
    if limit < 1 or limit > 200:
        raise ValueError("limit 必须在 1 到 200 之间")
    return limit


def _rows_to_hits(rows: Sequence[Mapping[Any, Any]]) -> tuple[SearchHit, ...]:
    return tuple(SearchHit.from_mapping(row) for row in rows)


def fuse_search_hits(
    channels: Mapping[str, Sequence[SearchHit]],
    *,
    rrf_k: int = 60,
    limit: int = 20,
    channel_weights: Mapping[str, float] | None = None,
) -> tuple[SearchHit, ...]:
    """融合检索命中，最终只应用一次 chunk.retrieval_weight。"""
    _validate_limit(limit)
    ranked = {
        channel: [
            RankedCandidate(
                chunk_id=hit.chunk_id,
                retrieval_weight=hit.retrieval_weight,
                payload=hit,
            )
            for hit in hits
        ]
        for channel, hits in channels.items()
    }
    fused = fuse_ranked_results(
        ranked,
        rrf_k=rrf_k,
        channel_weights=channel_weights,
    )
    return tuple(
        replace(
            item.payload,
            score=item.score,
            matched_channels=item.matched_channels,
        )
        for item in fused[:limit]
    )


class RetrievalService:
    """绑定独立 RAG engine 的同步检索服务。"""

    def __init__(
        self,
        *,
        settings: RAGSettings | None = None,
        engine: Engine | None = None,
    ) -> None:
        self.settings = settings or get_rag_settings()
        self.engine = engine or create_rag_engine(self.settings)
        self._owns_engine = engine is None

    def close(self) -> None:
        """释放服务自行创建的 engine。"""
        if self._owns_engine:
            self.engine.dispose()

    def _vector_search(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        filters: SearchFilters | None,
        exact: bool,
    ) -> tuple[SearchHit, ...]:
        query_vector = serialize_query_vector(
            vector,
            dimensions=self.settings.embedding_dimensions,
        )
        filter_sql, filter_params = build_filter_clause(filters)
        sql = text(
            f"""
            SELECT {_SELECT_COLUMNS},
                   1 - (c.embedding <=> CAST(:query_vector AS vector)) AS score
            FROM rag.chunks c
            WHERE c.corpus_id = (
                SELECT corpus_id FROM rag.corpus_versions WHERE status = 'active'
            )
            {filter_sql}
            ORDER BY c.embedding <=> CAST(:query_vector AS vector), c.chunk_id
            LIMIT :limit
            """
        )
        params = {"query_vector": query_vector, "limit": _validate_limit(limit), **filter_params}
        with self.engine.connect() as connection, connection.begin():
            if exact:
                connection.execute(text("SET LOCAL enable_indexscan = off"))
                connection.execute(text("SET LOCAL enable_bitmapscan = off"))
            else:
                ef_search = max(self.settings.hnsw_ef_search, limit)
                connection.execute(text(f"SET LOCAL hnsw.ef_search = {int(ef_search)}"))
            rows = connection.execute(sql, params).mappings().all()
        return _rows_to_hits(rows)

    def exact_vector_search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 20,
        filters: SearchFilters | None = None,
    ) -> tuple[SearchHit, ...]:
        """禁用索引扫描，执行精确 cosine distance 基线查询。"""
        return self._vector_search(vector, limit=limit, filters=filters, exact=True)

    def hnsw_vector_search(
        self,
        vector: Sequence[float],
        *,
        limit: int = 20,
        filters: SearchFilters | None = None,
    ) -> tuple[SearchHit, ...]:
        """使用 HNSW cosine 索引执行近似最近邻查询。"""
        return self._vector_search(vector, limit=limit, filters=filters, exact=False)

    def fts_search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: SearchFilters | None = None,
    ) -> tuple[SearchHit, ...]:
        """使用 PostgreSQL simple FTS 查询已预分词的中文 search_vector。"""
        tsquery = prepare_fts_query(query)
        filter_sql, filter_params = build_filter_clause(filters)
        sql = text(
            f"""
            WITH query AS (SELECT to_tsquery('simple', :tsquery) AS value)
            SELECT {_SELECT_COLUMNS},
                   ts_rank_cd(c.search_vector, query.value) AS score
            FROM rag.chunks c
            CROSS JOIN query
            WHERE c.corpus_id = (
                SELECT corpus_id FROM rag.corpus_versions WHERE status = 'active'
            )
              AND c.search_vector @@ query.value
            {filter_sql}
            ORDER BY score DESC, c.chunk_id
            LIMIT :limit
            """
        )
        params = {"tsquery": tsquery, "limit": _validate_limit(limit), **filter_params}
        with self.engine.connect() as connection:
            rows = connection.execute(sql, params).mappings().all()
        return _rows_to_hits(rows)

    def formula_search(
        self,
        query: str,
        *,
        limit: int = 20,
        filters: SearchFilters | None = None,
    ) -> tuple[SearchHit, ...]:
        """通过规范化公式 text[] 的 GIN overlap 执行精确公式召回。"""
        formula_terms = prepare_formula_query(query)
        if not formula_terms:
            return ()
        filter_sql, filter_params = build_filter_clause(filters)
        sql = text(
            f"""
            SELECT {_SELECT_COLUMNS}, 1.0::double precision AS score
            FROM rag.chunks c
            WHERE c.corpus_id = (
                SELECT corpus_id FROM rag.corpus_versions WHERE status = 'active'
            )
              AND c.formula_terms && CAST(:formula_terms AS text[])
            {filter_sql}
            ORDER BY c.retrieval_weight DESC, c.chunk_id
            LIMIT :limit
            """
        )
        params = {
            "formula_terms": list(formula_terms),
            "limit": _validate_limit(limit),
            **filter_params,
        }
        with self.engine.connect() as connection:
            rows = connection.execute(sql, params).mappings().all()
        return _rows_to_hits(rows)

    def hybrid_search(
        self,
        *,
        query_text: str,
        query_vector: Sequence[float],
        limit: int = 20,
        filters: SearchFilters | None = None,
        vector_candidates: int = 50,
        fts_candidates: int = 50,
        formula_candidates: int = 20,
    ) -> tuple[SearchHit, ...]:
        """按向量 Top50、FTS Top50、公式 Top20 的默认配置执行 RRF。"""
        channels: dict[str, Sequence[SearchHit]] = {
            "vector": self.hnsw_vector_search(
                query_vector,
                limit=vector_candidates,
                filters=filters,
            )
        }
        try:
            channels["fts"] = self.fts_search(query_text, limit=fts_candidates, filters=filters)
        except ValueError:
            pass
        formula_terms = prepare_formula_query(query_text)
        if formula_terms:
            channels["formula"] = self.formula_search(
                query_text,
                limit=formula_candidates,
                filters=filters,
            )
        return fuse_search_hits(
            channels,
            rrf_k=self.settings.rrf_k,
            limit=limit,
        )
