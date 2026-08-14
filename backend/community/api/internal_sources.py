"""Community 内部 Source Reader API（方案 §10.4，与 conversation internal_sources 对称）。

POST /api/v1/internal/community-sources/read：
- 只接受 actor_type=system 且持 community:source_read scope 的独立 principal
  （D36：system:community-reader）；普通用户/activity_agent/浏览器不可调用；
- 目标 user_id 来自请求体但必须由服务端做完整归属校验（§10.4）；
- 已删除来源返回 SOURCE_DELETED；不存在/归属不符/状态不符统一
  SOURCE_NOT_FOUND（不泄露对象状态）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from backend.auth.context import AuthContext
from backend.auth.verifier import AuthError
from backend.community import metrics
from backend.community.services.source_read_service import CommunitySourceReadService
from backend.memory.contracts.evidence import SourceBundle
from backend.shared.auth_context import get_auth_context

_SYSTEM_ONLY = frozenset({"system"})
_SCOPE_READ = "community:source_read"


class CommunitySourceReadRequest(BaseModel):
    """内部 Reader 请求体（§10.4）：user_id 为待读取目标，非调用者身份。"""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID
    activity_type: str
    activity_ids: list[str] = Field(min_length=1, max_length=50)
    content_ref: str | None = None


async def _require_reader_auth(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
    """内部端点鉴权（§10.4）：system principal + community:source_read。"""
    if auth.actor_type not in _SYSTEM_ONLY:
        raise AuthError(
            "AUTH_FORBIDDEN",
            "community-sources/read 仅限 system principal 调用",
            forbidden=True,
        )
    if not auth.has_scope(_SCOPE_READ):
        raise AuthError(
            "AUTH_FORBIDDEN",
            f"缺少 scope: {_SCOPE_READ}",
            forbidden=True,
        )
    return auth


def build_reader_router(service: CommunitySourceReadService) -> APIRouter:
    """组装带依赖的 Router（composition root 注入 service，§13.1）。"""

    router = APIRouter(prefix="/api/v1/internal/community-sources", tags=["internal-community"])

    @router.post("/read", response_model=SourceBundle)
    async def read_sources(
        request: CommunitySourceReadRequest,
        auth: AuthContext = Depends(_require_reader_auth),
    ) -> Any:
        """读取来源快照（§10.4）。目标 user_id 来自请求体，服务端完整归属校验。"""
        try:
            bundle = await service.read_source_bundle(
                user_id=request.user_id,
                activity_type=request.activity_type,
                activity_ids=request.activity_ids,
                content_ref=request.content_ref,
            )
        except Exception as exc:
            metrics.community_memory_source_read_total.labels(result=type(exc).__name__).inc()
            raise
        metrics.community_memory_source_read_total.labels(result="ok").inc()
        return bundle

    return router
