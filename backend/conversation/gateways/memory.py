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
