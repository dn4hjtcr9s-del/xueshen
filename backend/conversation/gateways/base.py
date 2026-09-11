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
        self,
        *,
        answer_context: dict[str, Any],
        tools: list[dict[str, Any]] | None = None,
        previous_response_id: str | None = None,
        tool_outputs: list[dict[str, Any]] | None = None,
    ) -> Any:
        """完整校验回答后返回应用层正文切片与生成结果（§19.2）。

        memory-rebuild §5.7：`tools is None` 时返回既有的
        `(deltas, payload)` 元组；`tools` 非空时返回工具轮结果
        （`deltas` / `payload` / `pending_tool_calls` / `response_id`）。
        这里刻意用 `Any` 表达联合返回：Protocol 无法用重载表达"参数决定返回类型"，
        而具体形态由 `OpenAIGateway` 实现类的 `@overload` 收口。
        """
        ...

    def supports_answer_streaming(self) -> bool:
        """是否启用真实 token 级流式（§15.4）；无此能力的网关走非流式。"""
        ...

    def open_answer_stream(
        self,
        *,
        answer_context: dict[str, Any],
        tools: list[dict[str, Any]] | None = None,
        previous_response_id: str | None = None,
        tool_outputs: list[dict[str, Any]] | None = None,
        tool_rounds: int = 0,
    ) -> Any:
        """打开真实流式回答会话（§15.4 / §5.7）；返回可异步迭代的流对象。"""
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

    async def search_memories(
        self,
        *,
        queries: list[str],
        match_mode: str = "any",
        max_results: int = 10,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """`memory.search`（memory-rebuild §2.4 D3①）：纯关键词定位，**不含正文**。"""
        ...

    async def read_memory(
        self,
        *,
        memory_id: str,
        line_offset: int = 0,
        max_lines: int = 200,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """`memory.read`（§2.4 D3②）：分段读取正文，带 version/checksum 供引用溯源。"""
        ...

    async def build_memory_prime(self, *, user_id: str | None = None) -> dict[str, Any]:
        """首轮 prime 输入（§2.4 D1）：summary 正文 + 注册表目录。"""
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
