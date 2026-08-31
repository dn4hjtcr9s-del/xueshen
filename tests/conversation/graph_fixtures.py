"""Conversation Graph 测试夹具（方案 §26.2：Fake OpenAI/Memory/Embedding/Retriever）。

生产不调用真实外部服务；Fake 通过脚本化队列/返回配置控制 Graph 行为。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from backend.conversation.contracts.graph import (
    RewritePlan,
)
from backend.conversation.contracts.retrieval import SearchHitRef
from backend.conversation.graph.state import (
    ConversationRuntimeContext,
    SystemClock,
    SystemIdGenerator,
)
from backend.conversation.persistence.event_writer import TurnEventWriter


class FakeOpenAIGateway:
    """Fake OpenAI Gateway：脚本化结果队列（§26.2）。"""

    def __init__(self) -> None:
        self.rewrite_queue: list[dict[str, Any]] = []
        self.assess_queue: list[dict[str, Any]] = []
        self.answer_payloads: list[dict[str, Any]] = []
        self.summary_results: list[str] = []
        self.records: list[dict[str, Any]] = []

    async def rewrite_and_plan(
        self, *, context_view: dict[str, Any], prior_attempts: int
    ) -> dict[str, Any]:
        self.records.append({"call": "rewrite", "attempts": prior_attempts})
        if not self.rewrite_queue:
            raise RuntimeError("Fake rewrite 队列已空")
        return self.rewrite_queue.pop(0)

    async def assess_evidence(
        self, *, question: str, evidence_summary: str, budget_remaining: str
    ) -> dict[str, Any]:
        self.records.append({"call": "assess"})
        if not self.assess_queue:
            raise RuntimeError("Fake assess 队列已空")
        return self.assess_queue.pop(0)

    async def stream_answer(
        self, *, answer_context: dict[str, Any]
    ) -> tuple[list[str], dict[str, Any]]:
        self.records.append({"call": "answer"})
        if not self.answer_payloads:
            raise RuntimeError("Fake answer 队列已空")
        payload = self.answer_payloads.pop(0)
        deltas = list(payload.get("answer", ""))
        return deltas, payload


class ValidatedAnswerOpenAIGateway(FakeOpenAIGateway):
    """模拟完整结构化校验后由应用层切分正文的网关行为。"""

    async def stream_answer(
        self, *, answer_context: dict[str, Any]
    ) -> tuple[list[str], dict[str, Any]]:
        from backend.conversation.contracts.graph import AnswerPayload

        self.records.append({"call": "answer"})
        if not self.answer_payloads:
            raise RuntimeError("Fake answer 队列已空")
        payload = self.answer_payloads.pop(0)
        parsed = AnswerPayload.model_validate(payload)
        deltas = list(parsed.answer)
        return deltas, parsed.model_dump(mode="json")

    async def summarize_conversation(
        self, *, messages: list[dict[str, Any]], previous_summary: str | None
    ) -> str:
        self.records.append({"call": "summary"})
        if not self.summary_results:
            return "（摘要）"
        return self.summary_results.pop(0)


class FakeMemoryGateway:
    def __init__(self) -> None:
        self.context_results: list[dict[str, Any]] = []
        self.evidence_results: list[dict[str, Any]] = []
        self.errors: list[Exception] = []

    async def build_learning_context(
        self,
        *,
        query: str,
        token_budget: int | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        if self.errors:
            raise self.errors.pop(0)
        if not self.context_results:
            return {"status": "unavailable", "truncated": False}
        return self.context_results.pop(0)

    async def submit_conversation_evidence(self, **kwargs: Any) -> dict[str, Any]:
        if self.errors:
            raise self.errors.pop(0)
        if not self.evidence_results:
            return {"operation_id": str(uuid4()), "status": "succeeded"}
        return self.evidence_results.pop(0)


class FakeEmbeddingGateway:
    def __init__(self) -> None:
        self.vectors: list[list[float]] = []
        self.errors: list[Exception] = []

    async def embed(self, *, texts: list[str]) -> list[list[float]]:
        if self.errors:
            raise self.errors.pop(0)
        return [self.vectors[i] if i < len(self.vectors) else [0.1, 0.2] for i in range(len(texts))]


class FakeRetrieverGateway:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []

    async def retrieve(
        self,
        *,
        query_text: str,
        query_vector: list[float] | None,
        filters: dict[str, list[str]] | None,
        limit: int,
        deadline: Any | None = None,
    ) -> dict[str, Any]:
        if self.results:
            return self.results.pop(0)
        return {
            "worker_key": "",
            "subquery_id": "",
            "normalized_query": query_text,
            "status": "succeeded",
            "hits": [],
            "latency_ms": 1.0,
            "attempt_count": 1,
            "error_code": None,
        }


def make_hit(
    *,
    chunk_id: str = "chunk-1",
    corpus_id: str = "corpus-a",
    chunk_index: int = 0,
    score: float = 0.9,
    content: str = "勾股定理：直角三角形两直角边的平方和等于斜边的平方。",
    book_id: str = "book-1",
    book_name: str = "初中数学",
) -> dict[str, Any]:
    ref = SearchHitRef(
        chunk_id=chunk_id,
        corpus_id=corpus_id,
        chunk_index=chunk_index,
        token_count=10,
        score=score,
        book_id=book_id,
        book_name=book_name,
        grade_level="7",
        section="几何",
        chapter_path=("第一章",),
        content_role="theorem",
        content_text=content,
        source_page_start=1,
        source_page_end=2,
        source_refs=({"source": "book"},),
    )
    return ref.__dict__


def default_rewrite_plan(*, subqueries: int = 1, need_retrieval: bool = True) -> dict[str, Any]:
    plan = RewritePlan(
        plan_revision=0,
        standalone_question="勾股定理是什么？",
        answer_mode="rag" if need_retrieval else "direct",
        need_retrieval=need_retrieval,
        subqueries=[
            {
                "subquery_id": f"sq-{i}",
                "query_text": "勾股定理",
                "intent": "definition",
                "coverage_target": "",
                "semantic_filters": {},
            }
            for i in range(subqueries)
        ],
    )
    return plan.model_dump(mode="json")


def build_runtime(**overrides: Any) -> ConversationRuntimeContext:
    """构造带 Fake Gateway 的 runtime（单测直注辅助对象）。"""
    from backend.conversation.persistence.repository import ConversationRepository
    from backend.conversation.services.context_service import ContextService
    from backend.conversation.services.token_counter import TokenCounter, WhitespaceTokenizer
    from backend.settings import Settings

    settings = Settings(app_env="test")
    logger = logging.getLogger("conversation.test")
    runtime = ConversationRuntimeContext(
        openai_gateway=overrides.get("openai_gateway", FakeOpenAIGateway()),
        memory_gateway=overrides.get("memory_gateway", FakeMemoryGateway()),
        embedding_gateway=overrides.get("embedding_gateway", FakeEmbeddingGateway()),
        retriever_gateway=overrides.get("retriever_gateway", FakeRetrieverGateway()),
        conversation_repository=overrides.get(
            "conversation_repository",
            ConversationRepository(session_factory=overrides.get("session_factory")),
        ),
        turn_event_writer=overrides.get(
            "turn_event_writer", TurnEventWriter(id_generator=SystemIdGenerator())
        ),
        clock=overrides.get("clock", SystemClock()),
        id_generator=overrides.get("id_generator", SystemIdGenerator()),
        logger=logger,
        flags=overrides.get("flags", settings.conversation_flags),
        worker_id=overrides.get("worker_id", "test-worker"),
    )
    token_counter = TokenCounter(tokenizer=WhitespaceTokenizer())
    runtime.context_service = ContextService(settings=settings, token_counter=token_counter)
    runtime.settings = settings
    runtime.token_counter = token_counter
    return runtime


def make_evidence_hit_dict(**overrides: Any) -> dict[str, Any]:
    """构造 SearchHit dict（直接用于 worker_results）。"""
    hit = make_hit(**overrides)
    return hit
