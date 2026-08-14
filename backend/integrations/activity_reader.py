"""HttpActivityReader：Memory 侧生产 ActivityReader 适配器（方案 community §10.4）。

Memory Worker 是独立进程，通过受认证的内部 HTTP Reader API
POST /api/v1/internal/community-sources/read 读取社区证据正文（与
HttpConversationReader 对称）。使用独立 system principal
（COMMUNITY_READER_SERVICE_TOKEN，D36：system:community-reader）和
community:source_read scope（§13.3：加入 ALL_SCOPES、不入 AGENT_ALLOWED_SCOPES）。

请求体含 user_id 时 Community 服务只把它当作"待读取目标"，执行完整归属
校验（§10.4），不因 system principal 放行所有用户数据。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx

from backend.memory.contracts.evidence import SourceBundle


class HttpActivityReader:
    """ActivityReader Protocol 的 HTTP 实现（§10.4，对齐 conversation_reader.py）。"""

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

    async def __aenter__(self) -> HttpActivityReader:
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
        activity_type: str,
        activity_ids: list[str],
        content_ref: str | None,
    ) -> SourceBundle:
        """读取社区来源快照（§10.4）；HTTP 错误映射为 Memory 域错误语义。"""
        from backend.memory.contracts.errors import (
            SourceDeletedError,
            SourceNotFoundError,
            SourceTooLargeError,
        )

        payload: dict[str, Any] = {
            "user_id": str(user_id),
            "activity_type": activity_type,
            "activity_ids": list(activity_ids),
        }
        if content_ref is not None:
            payload["content_ref"] = content_ref
        response = await self._http.post(
            "/api/v1/internal/community-sources/read",
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
