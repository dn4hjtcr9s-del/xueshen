"""HttpConversationReader：Memory 侧生产 ConversationReader 适配器（方案 §8.2）。

Memory Worker 是独立进程，通过受认证的内部 HTTP Reader API
POST /api/v1/internal/conversation-sources/read 读取对话证据正文，
避免 Memory 进程获得 Conversation DB 凭证。使用独立 system principal
（CONVERSATION_READER_SERVICE_TOKEN）和 conversation:source_read scope。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx

from backend.memory.contracts.evidence import SourceBundle


class HttpConversationReader:
    """ConversationReader Protocol 的 HTTP 实现（§8.2）。

    token_provider 每次请求调用获取短时 JWT（与 MemoryClient 同模式）。
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

    async def __aenter__(self) -> HttpConversationReader:
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

    async def read(
        self,
        *,
        user_id: UUID,
        thread_id: str,
        checkpoint_id: str | None,
        message_ids: list[str],
    ) -> SourceBundle:
        """读取来源快照（§8.2）；HTTP 错误映射为 Memory 域错误语义。"""
        from backend.memory.contracts.errors import (
            SourceDeletedError,
            SourceNotFoundError,
            SourceTooLargeError,
        )

        payload: dict[str, Any] = {
            "user_id": str(user_id),
            "thread_id": thread_id,
            "message_ids": list(message_ids),
        }
        if checkpoint_id is not None:
            payload["checkpoint_id"] = checkpoint_id
        response = await self._http.post(
            "/api/v1/internal/conversation-sources/read",
            json=payload,
            headers={"Authorization": self._authorization()},
        )
        if response.status_code >= 400:
            error_code = "INTERNAL_ERROR"
            try:
                error = response.json().get("error", {})
                error_code = str(error.get("code", error_code))
            except ValueError:
                pass
            if error_code == "SOURCE_DELETED":
                raise SourceDeletedError("来源已被删除")
            if error_code in ("SOURCE_NOT_FOUND", "SOURCE_ACCESS_DENIED"):
                raise SourceNotFoundError("来源不存在或无权访问")
            if error_code == "SOURCE_TOO_LARGE":
                raise SourceTooLargeError("来源超过大小上限")
            raise SourceNotFoundError("来源读取失败")
        return SourceBundle.model_validate(response.json())
