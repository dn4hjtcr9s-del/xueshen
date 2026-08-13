"""AsyncRetrieverAdapter（方案 §12.2 / §4.1）。

现有 RetrievalService 是同步 SQLAlchemy 服务，不能直接在 FastAPI/LangGraph
Event Loop 中并发调用。第一版通过受控线程池适配：

    async retrieve(...)
      → bounded semaphore
      → dedicated executor
      → RetrievalService.hybrid_search(...)
      → timeout / cancellation / normalized error

以后可以在不改变 Graph Protocol 的情况下替换为异步检索实现（§12.2）。
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from backend.conversation.contracts.retrieval import (
    RetrievalWorkerResult,
    SearchHitRef,
)
from backend.rag.retrieval import RetrievalService
from backend.rag.schemas import SearchFilters


class AsyncRetrieverAdapter:
    """同步 RetrievalService 的异步线程池适配（§12.2）。"""

    def __init__(
        self,
        *,
        retrieval_service: RetrievalService,
        concurrency: int = 4,
        worker_timeout_seconds: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._service = retrieval_service
        self._semaphore = asyncio.Semaphore(concurrency)
        self._timeout = worker_timeout_seconds
        self._logger = logger or logging.getLogger("conversation.gateways.retriever")
        self._concurrency = concurrency
        self._executor: ThreadPoolExecutor | None = None  # 惰性创建，避免未运行事件循环时构造

    async def retrieve(
        self,
        *,
        query_text: str,
        query_vector: list[float] | None,
        filters: dict[str, list[str]] | None,
        limit: int,
        deadline: Any | None = None,
    ) -> dict[str, Any]:
        """hybrid_search 异步适配（§12.2）；返回 RetrievalWorkerResult dict。"""
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=max(self._concurrency, 1),
                thread_name_prefix="conv-retriever",
            )
        search_filters = _to_search_filters(filters)
        loop = asyncio.get_running_loop()
        started = loop.time()
        async with self._semaphore:
            try:
                hits = await asyncio.wait_for(
                    loop.run_in_executor(
                        self._executor,
                        lambda: self._service.hybrid_search(
                            query_text=query_text,
                            query_vector=query_vector or [],
                            limit=limit,
                            filters=search_filters,
                        ),
                    ),
                    timeout=self._timeout,
                )
            except TimeoutError:
                return RetrievalWorkerResult(
                    worker_key="",
                    subquery_id="",
                    normalized_query=query_text,
                    status="timed_out",
                    error_code="RETRIEVAL_TIMED_OUT",
                ).__dict__ | {"worker_key": ""}
            except Exception as exc:
                self._logger.warning("检索 Worker 失败: %s", type(exc).__name__)
                return RetrievalWorkerResult(
                    worker_key="",
                    subquery_id="",
                    normalized_query=query_text,
                    status="failed",
                    error_code="RETRIEVAL_FAILED",
                ).__dict__ | {"worker_key": ""}
        latency_ms = (loop.time() - started) * 1000
        refs = tuple(_to_hit_ref(hit) for hit in hits)
        return {
            "worker_key": "",
            "subquery_id": "",
            "normalized_query": query_text,
            "status": "succeeded",
            "hits": refs,
            "latency_ms": latency_ms,
            "attempt_count": 1,
            "error_code": None,
        }


def _to_search_filters(filters: dict[str, list[str]] | None) -> SearchFilters | None:
    if not filters:
        return None
    return SearchFilters(
        book_ids=tuple(filters.get("book_ids", ())),
        grade_levels=tuple(filters.get("grade_levels", ())),
        sections=tuple(filters.get("sections", ())),
        content_roles=tuple(filters.get("content_roles", ())),
        chapter_prefix=tuple(filters.get("chapter_prefix", ())),
    )


def _to_hit_ref(hit: Any) -> SearchHitRef:
    """SearchHit → 不可变引用（§12.5 增强列已由 SearchHit 携带）。"""
    return SearchHitRef(
        chunk_id=hit.chunk_id,
        corpus_id=hit.corpus_id,
        chunk_index=hit.chunk_index,
        token_count=hit.token_count,
        score=hit.score,
        book_id=hit.book_id,
        book_name=hit.book_name,
        grade_level=hit.grade_level,
        section=hit.section,
        chapter_path=hit.chapter_path,
        content_role=hit.content_role,
        content_text=hit.content_text,
        source_page_start=hit.source_page_start,
        source_page_end=hit.source_page_end,
        source_refs=hit.source_refs,
    )
