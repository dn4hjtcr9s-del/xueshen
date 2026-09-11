"""MemoryGateway：包装既有 MemoryClient（方案 §16）。

主路径 build_learning_context()；只有产品明确需要额外全文 Memory 搜索时才调用
search_summary()（§16.1）。图谱推荐优先从 LearningContext.recommendations 使用。
错误映射（§16.2）：超时/5xx → unavailable 快照继续；认证/权限 → Turn 失败；
4xx 契约错误 → Turn 失败并告警，禁止当普通降级处理。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.conversation.contracts.errors import MemoryUnavailableError
from backend.memory.client import MemoryClient, MemoryClientError


class MemoryGateway:
    """Conversation 域 Memory Gateway（Real 实现，组合 root 注入 MemoryClient）。"""

    def __init__(
        self,
        *,
        client: MemoryClient,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._logger = logger or logging.getLogger("conversation.gateways.memory")

    async def build_learning_context(
        self,
        *,
        query: str,
        token_budget: int | None = None,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """读取长期记忆（§16.1 / 第三轮必改 4）。

        失败统一抛 MemoryUnavailableError，但透传底层 HTTP 状态到
        source_http_status，供节点区分 4xx（Turn 失败）与 5xx/网络（降级）。
        """
        try:
            context = await self._client.build_learning_context(
                query=query, token_budget=token_budget, user_id=user_id
            )
        except MemoryClientError as exc:
            self._logger.warning(
                "Memory build_learning_context 失败: code=%s http=%s",
                exc.code,
                exc.http_status,
            )
            raise MemoryUnavailableError(
                f"Memory 读取失败: {exc.code}",
                source_http_status=exc.http_status,
            ) from exc
        except Exception as exc:
            self._logger.warning("Memory 连接异常: %s", type(exc).__name__)
            raise MemoryUnavailableError("Memory 读取不可用") from exc
        return context.model_dump(mode="json")

    async def search_memories(
        self,
        *,
        queries: list[str],
        match_mode: str = "any",
        max_results: int = 10,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """`memory.search`（§2.4 D3）：纯关键词定位，返回注册表条目、**不含正文**。

        失败统一抛 :class:`MemoryUnavailableError` 并透传 HTTP 状态：工具失败按
        §16.2 的降级语义处理（5xx/网络 → 本轮工具不可用，继续回答；4xx 契约错误
        → 视为实现缺陷，交由调用方决定是否让 Turn 失败）。
        """
        return await self._call_tool(
            "memory.search",
            self._client.memory_tool_search,
            queries=queries,
            match_mode=match_mode,
            max_results=max_results,
            user_id=user_id,
        )

    async def read_memory(
        self,
        *,
        memory_id: str,
        line_offset: int = 0,
        max_lines: int = 200,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """`memory.read`（§2.4 D3）：分段读取正文，带 version/checksum 供引用溯源。"""
        return await self._call_tool(
            "memory.read",
            self._client.memory_tool_read,
            memory_id=memory_id,
            line_offset=line_offset,
            max_lines=max_lines,
            user_id=user_id,
        )

    async def build_memory_prime(self, *, user_id: str | None = None) -> dict[str, Any]:
        """首轮 prime 输入（§2.4 D1）：摘要全文 + 注册表目录。

        summary 缺失/损坏时服务端会返回 `degraded=true` 而不是报错，因此这里**不**
        把它当失败处理——prime 为空仍是合法状态，只需记录降级标记。
        """
        return await self._call_tool(
            "memory.prime", self._client.memory_tool_prime, user_id=user_id
        )

    async def _call_tool(self, tool: str, call: Any, **kwargs: Any) -> dict[str, Any]:
        """工具调用的统一错误映射（供 search / read / prime 复用）。"""
        try:
            result = await call(**kwargs)
        except MemoryClientError as exc:
            self._logger.warning("%s 失败: code=%s http=%s", tool, exc.code, exc.http_status)
            raise MemoryUnavailableError(
                f"{tool} 失败: {exc.code}", source_http_status=exc.http_status
            ) from exc
        except Exception as exc:
            self._logger.warning("%s 连接异常: %s", tool, type(exc).__name__)
            raise MemoryUnavailableError(f"{tool} 不可用") from exc
        if not isinstance(result, dict):
            raise MemoryUnavailableError(f"{tool} 返回体类型异常")
        return result

    async def submit_conversation_evidence(self, **kwargs: Any) -> dict[str, Any]:
        """提交对话证据（§16.3/§16.4）；返回 MemoryOperationResult dict。"""
        try:
            result = await self._client.submit_conversation_evidence(**kwargs)
        except MemoryClientError as exc:
            self._logger.warning(
                "Memory submit_conversation_evidence 失败: code=%s http=%s",
                exc.code,
                exc.http_status,
            )
            raise MemoryUnavailableError(
                f"Memory 投递失败: {exc.code}",
                source_http_status=exc.http_status,
            ) from exc
        return result.model_dump(mode="json")
