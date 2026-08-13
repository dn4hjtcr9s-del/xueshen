"""内部 Reader API：POST /api/v1/internal/conversation-sources/read（方案 §8.2）。

只接受具有 conversation:source_read 的独立 system principal（actor_type=system）；
目标 user_id 来自请求体但必须由服务端做完整归属校验（§8.3 #1 / §22）。
浏览器和 delegated Agent 不得调用；未获 owner 批准前由部署配置决定是否挂载
（默认关闭，见 A.11 决策 1）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from backend.auth.context import AuthContext
from backend.auth.verifier import AuthError
from backend.conversation.services.source_read_service import ConversationSourceReadService
from backend.memory.api.dependencies import get_auth_context
from backend.memory.contracts.evidence import SourceBundle

_SYSTEM_ONLY = frozenset({"system"})


class SourceReadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    thread_id: str = Field(min_length=1, max_length=200)
    checkpoint_id: str | None = Field(default=None, max_length=500)
    message_ids: list[str] = Field(min_length=1, max_length=200)


async def _require_reader_auth(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
    """内部端点鉴权（§8.2 / §22 #2）：system principal + conversation:source_read。"""
    if auth.actor_type not in _SYSTEM_ONLY:
        raise AuthError(
            "AUTH_FORBIDDEN",
            "conversation-sources/read 仅限 system principal 调用",
            forbidden=True,
        )
    if not auth.has_scope("conversation:source_read"):
        raise AuthError(
            "AUTH_FORBIDDEN",
            "缺少 scope: conversation:source_read",
            forbidden=True,
        )
    return auth


def build_reader_router(service: ConversationSourceReadService) -> APIRouter:
    """组装带依赖的 Router（composition root 注入 service）。"""

    router = APIRouter(
        prefix="/api/v1/internal/conversation-sources", tags=["internal-conversation"]
    )

    @router.post("/read", response_model=SourceBundle)
    async def read_sources(
        request: SourceReadRequest,
        auth: AuthContext = Depends(_require_reader_auth),
    ) -> Any:
        """读取来源快照（§8.2）。目标 user_id 来自请求体，服务端完整归属校验。"""
        return await service.read_source_bundle(
            user_id=request.user_id,
            thread_id=request.thread_id,
            checkpoint_id=request.checkpoint_id,
            message_ids=request.message_ids,
        )

    return router
