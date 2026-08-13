"""ActiveCorpusVocabulary 加载器（方案 Q9 / D15 / 评审 P1-10）。

active corpus 合法过滤词表从 RAG 库（books/chunks 表）去重生成并版本化，
注入 RewriteContextView 供 rewrite prompt 使用（§9.4 / §11.1）。
启动时强校验 Embedding 模型标识与维度与现有 RAG artifact 一致（§12.1 #3）；
不一致时 readiness 失败，禁止带错维度运行。
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.conversation.contracts.retrieval import (
    FILTER_VOCABULARY_VERSION,
    ActiveCorpusVocabulary,
)


class ActiveCorpusVocabularyLoader:
    """从 RAG 库读取 active corpus 词表（只读；独立 engine 装配）。"""

    def __init__(self, rag_engine: Engine) -> None:
        self._engine = rag_engine

    def load(self) -> ActiveCorpusVocabulary:
        """读取 active corpus 的合法词表（Q9：模型只能选合法值，服务端仍 allow-list）。"""
        with self._engine.connect() as connection:
            corpus = (
                connection.execute(
                    text(
                        "SELECT corpus_id, embedding_model, embedding_dimensions "
                        "FROM rag.corpus_versions WHERE status = 'active' LIMIT 1"
                    )
                )
                .mappings()
                .first()
            )
            if corpus is None:
                return ActiveCorpusVocabulary()
            books = (
                connection.execute(
                    text(
                        "SELECT book_id, grade_level FROM rag.books "
                        "WHERE corpus_id = :corpus_id ORDER BY book_id"
                    ),
                    {"corpus_id": corpus["corpus_id"]},
                )
                .mappings()
                .all()
            )
            sections = (
                connection.execute(
                    text(
                        "SELECT DISTINCT section FROM rag.chunks "
                        "WHERE corpus_id = :corpus_id AND section IS NOT NULL ORDER BY section"
                    ),
                    {"corpus_id": corpus["corpus_id"]},
                )
                .mappings()
                .all()
            )
            roles = (
                connection.execute(
                    text(
                        "SELECT DISTINCT content_role FROM rag.chunks "
                        "WHERE corpus_id = :corpus_id ORDER BY content_role"
                    ),
                    {"corpus_id": corpus["corpus_id"]},
                )
                .mappings()
                .all()
            )
            prefixes = (
                connection.execute(
                    text(
                        "SELECT DISTINCT chapter_path FROM rag.chunks "
                        "WHERE corpus_id = :corpus_id AND chapter_path IS NOT NULL "
                        "ORDER BY chapter_path LIMIT 200"
                    ),
                    {"corpus_id": corpus["corpus_id"]},
                )
                .mappings()
                .all()
            )
        return ActiveCorpusVocabulary(
            version=FILTER_VOCABULARY_VERSION,
            allowed_book_ids=tuple(str(b["book_id"]) for b in books),
            allowed_grade_levels=tuple(
                sorted({str(b["grade_level"]) for b in books if b["grade_level"]})
            ),
            allowed_sections=tuple(str(s["section"]) for s in sections),
            allowed_content_roles=tuple(str(r["content_role"]) for r in roles),
            allowed_chapter_prefixes=tuple(
                str(p["chapter_path"][0]) for p in prefixes if p["chapter_path"]
            ),
        )

    def validate_embedding_profile(self, *, model: str | None, dimensions: int) -> list[str]:
        """D15 / 评审 P1-10：启动时与 active corpus manifest 强校验。

        返回失败项列表（空 = 通过）。模型标识与维度不一致 → readiness 失败。
        """
        failures: list[str] = []
        with self._engine.connect() as connection:
            corpus = (
                connection.execute(
                    text(
                        "SELECT embedding_model, embedding_dimensions "
                        "FROM rag.corpus_versions WHERE status = 'active' LIMIT 1"
                    )
                )
                .mappings()
                .first()
            )
        if corpus is None:
            return failures
        if model and corpus["embedding_model"] and model != corpus["embedding_model"]:
            failures.append(
                f"embedding_model 不匹配: 配置 {model} != corpus {corpus['embedding_model']}"
            )
        if dimensions and int(corpus["embedding_dimensions"]) != dimensions:
            failures.append(
                f"embedding_dimensions 不匹配: 配置 {dimensions} "
                f"!= corpus {corpus['embedding_dimensions']}"
            )
        return failures
