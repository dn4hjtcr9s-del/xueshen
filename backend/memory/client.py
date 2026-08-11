"""MemoryClient：供内部 Agent 使用的 HTTP 客户端（规格 §19.8）。

第一版使用带服务 JWT 的 HTTP 实现，不提供 MCP。Agent 不依赖 HTTP 路由细节。
search_summary / build_learning_context / get_graph_recommendations 分别承载于
POST /memory/search、POST /memory/context、GET /knowledge-graph/recommendations。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, Self
from uuid import UUID

import httpx

from backend.memory.contracts.context import LearningContext, LearningContextRequest
from backend.memory.contracts.evidence import ActivityEvidence, ConversationEvidence
from backend.memory.contracts.graph_state import GraphRecommendation
from backend.memory.contracts.operations import MemoryOperationResult
from backend.memory.contracts.results import MemorySearchHit, MemorySearchRequest


class MemoryClientError(Exception):
    """API 返回的业务错误（§7.3 PublicError）。"""

    def __init__(
        self, code: str, message: str, *, http_status: int, trace_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.trace_id = trace_id


class MemoryClient:
    """带服务 JWT 的 Memory API 客户端。

    token_provider 每次请求调用以获取短时 JWT（§18.1：token 最长 5 分钟）。
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        token_provider: Callable[[], str] | None = None,
        http: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        if token is None and token_provider is None:
            raise ValueError("必须提供 token 或 token_provider")
        self._token = token
        self._token_provider = token_provider
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _authorization(self) -> str:
        token = self._token_provider() if self._token_provider else self._token
        assert token is not None
        return f"Bearer {token}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        headers = {"Authorization": self._authorization()}
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        response = await self._http.request(
            method, path, json=json_body, params=params, headers=headers
        )
        if response.status_code >= 400:
            try:
                error = response.json().get("error", {})
            except ValueError:
                error = {}
            raise MemoryClientError(
                str(error.get("code", "INTERNAL_ERROR")),
                str(error.get("message", response.text[:200])),
                http_status=response.status_code,
                trace_id=error.get("trace_id"),
            )
        return response.json()

    async def submit_conversation_evidence(
        self,
        *,
        idempotency_key: str,
        thread_id: str,
        message_ids: list[str],
        trigger: Literal[
            "explicit_remember",
            "turn_boundary",
            "topic_switch",
            "exercise_completed",
            "conversation_end",
        ],
        checkpoint_id: str | None = None,
        topic_hints: list[str] | None = None,
        graph_node_hints: list[str] | None = None,
    ) -> MemoryOperationResult:
        """提交对话证据（§19.1）；返回 202 的 operation 结果，轮询 get_operation。"""
        evidence = ConversationEvidence(
            thread_id=thread_id,
            checkpoint_id=checkpoint_id,
            message_ids=message_ids,
            trigger=trigger,
            topic_hints=topic_hints or [],
            graph_node_hints=graph_node_hints or [],
        )
        data = await self._request(
            "POST",
            "/api/v1/memory/events",
            json_body=evidence.model_dump(mode="json"),
            idempotency_key=idempotency_key,
        )
        return MemoryOperationResult.model_validate(data)

    async def submit_activity_evidence(
        self,
        *,
        idempotency_key: str,
        activity_type: Literal[
            "forum_post",
            "forum_reply",
            "wrong_question_upload",
            "exercise_attempt",
            "review_result",
            "page_view",
            "bookmark",
            "check_in",
        ],
        activity_ids: list[str],
        content_ref: str | None = None,
        aggregated_count: int = 1,
        window_started_at: datetime | None = None,
        window_ended_at: datetime | None = None,
        topic_hints: list[str] | None = None,
        graph_node_hints: list[str] | None = None,
    ) -> MemoryOperationResult:
        """提交行为证据（§19.1）。"""
        evidence = ActivityEvidence(
            activity_type=activity_type,
            activity_ids=activity_ids,
            content_ref=content_ref,
            aggregated_count=aggregated_count,
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
            topic_hints=topic_hints or [],
            graph_node_hints=graph_node_hints or [],
        )
        data = await self._request(
            "POST",
            "/api/v1/memory/events",
            json_body=evidence.model_dump(mode="json"),
            idempotency_key=idempotency_key,
        )
        return MemoryOperationResult.model_validate(data)

    async def get_operation(self, operation_id: UUID) -> MemoryOperationResult:
        """轮询 operation 状态（§19.3 / §20.3）。"""
        data = await self._request("GET", f"/api/v1/memory/operations/{operation_id}")
        return MemoryOperationResult.model_validate(data)

    async def cancel_operation(
        self, operation_id: UUID, *, idempotency_key: str
    ) -> MemoryOperationResult:
        """取消 operation（§11.6）；terminal 状态返回 409 并抛 MemoryClientError。"""
        data = await self._request(
            "POST",
            f"/api/v1/memory/operations/{operation_id}/cancel",
            idempotency_key=idempotency_key,
        )
        return MemoryOperationResult.model_validate(data)

    async def search_summary(
        self,
        *,
        query: str,
        topic_keys: list[str] | None = None,
        memory_types: list[Literal["learner", "mastery"]] | None = None,
        limit: int = 10,
    ) -> list[MemorySearchHit]:
        """检索总结记忆（§12.2/§19.4）；返回首页命中（limit ≤ 50）。"""
        body = MemorySearchRequest(
            query=query,
            topic_keys=topic_keys or [],
            memory_types=memory_types or [],
            limit=limit,
        )
        data = await self._request(
            "POST", "/api/v1/memory/search", json_body=body.model_dump(mode="json")
        )
        return [MemorySearchHit.model_validate(item) for item in data["items"]]

    async def build_learning_context(
        self,
        *,
        query: str,
        topic_keys: list[str] | None = None,
        token_budget: int | None = None,
    ) -> LearningContext:
        """组装学习上下文（§12.4/§12.5）；token_budget 省略时服务端默认 3000。"""
        body = LearningContextRequest(
            query=query, topic_keys=topic_keys or [], token_budget=token_budget
        )
        data = await self._request(
            "POST", "/api/v1/memory/context", json_body=body.model_dump(mode="json")
        )
        return LearningContext.model_validate(data)

    async def get_graph_recommendations(
        self, *, cursor: str | None = None, limit: int = 20
    ) -> list[GraphRecommendation]:
        """图谱推荐（§16.5）；返回一页（默认 20、最大 50），翻页传 cursor。"""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        data = await self._request("GET", "/api/v1/knowledge-graph/recommendations", params=params)
        return [GraphRecommendation.model_validate(item) for item in data["items"]]
