"""Conversation Gateway 协议与公共错误映射（方案 §4.1 / §19）。

graph 只依赖 Gateway Protocol，不依赖 OpenAI、Memory、SQLAlchemy 或 RAG
的具体客户端；所有具体客户端、连接池和凭证只在 composition root 装配。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class OpenAIGateway(Protocol):
    """OpenAI SDK Gateway（§19.2）。"""

    async def rewrite_and_plan(
        self, *, context_view: dict[str, Any], prior_attempts: int
    ) -> dict[str, Any]:
        """rewrite_and_plan(snapshot, prior_attempts) -> RewritePlan（§19.2）。"""
        ...

    async def assess_evidence(
        self, *, question: str, evidence_summary: str, budget_remaining: str
    ) -> dict[str, Any]:
        """assess_evidence(question, evidence, budget) -> EvidenceAssessment（§19.2）。"""
        ...

    async def stream_answer(
        self, *, answer_context: dict[str, Any]
    ) -> tuple[list[str], dict[str, Any]]:
        """完整校验回答后返回应用层正文切片与生成结果（§19.2）。"""
        ...

    async def summarize_conversation(
        self, *, messages: list[dict[str, Any]], previous_summary: str | None
    ) -> str:
        """summarize_conversation(messages, previous_summary) -> summary（§19.2）。"""
        ...


@runtime_checkable
class MemoryGateway(Protocol):
    """MemoryGateway（§16.1）：包装既有 MemoryClient。"""

    async def build_learning_context(
        self,
        *,
        query: str,
        token_budget: int | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """读取长期记忆（§16.1）；返回 LearningContext dict 或抛域错误。"""
        ...

    async def submit_conversation_evidence(self, **kwargs: Any) -> dict[str, Any]:
        """提交对话证据（§16.3/§16.4）；返回 MemoryOperationResult dict。"""
        ...


@runtime_checkable
class QueryEmbeddingGateway(Protocol):
    """Query Embedding Gateway（§12.1）。"""

    async def embed(self, *, texts: list[str]) -> list[list[float]]:
        """批量生成查询向量，按输入顺序返回（§12.1 #4）。"""
        ...


@runtime_checkable
class RetrieverGateway(Protocol):
    """Retriever Gateway（§4.1 / §12.2）：AsyncRetrieverAdapter 边界。"""

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
        ...
