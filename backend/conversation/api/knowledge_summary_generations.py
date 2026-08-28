"""知识总结 Generation API（知识总结方案 §15.8–§15.11）。

读取主开关开启时暴露当前 Job 和单 Job 状态查询；生成开关开启时额外暴露手动生成与
review dismiss 写路径。这样 generation 关闭时已有总结仍可显示历史状态，但不能创建新 Job。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request

from backend.auth.context import AuthContext
from backend.conversation.api.dependencies import (
    ConversationApiContext,
    get_conversation_context,
)
from backend.conversation.contracts.knowledge_summary import (
    CreateKnowledgeSummaryGenerationRequest,
    CurrentTurnKnowledgeSummaryGenerationResponse,
    DismissReviewRequest,
    KnowledgeSummaryGenerationResponse,
    KnowledgeSummaryGenerationStatusResponse,
)
from backend.conversation.services.knowledge_summary_generation_api import (
    KnowledgeSummaryGenerationApiService,
)
from backend.memory.api.dependencies import get_auth_context
from backend.shared.client_ip import client_ip


def _client_ip(request: Request, ctx: ConversationApiContext) -> str | None:
    """使用 Conversation 独立可信代理配置解析客户端 IP。"""
    return client_ip(request, ctx.settings.conversation_trusted_proxy_cidrs)


def build_knowledge_summary_generation_read_router(
    service: KnowledgeSummaryGenerationApiService,
) -> APIRouter:
    """创建历史 Generation 只读路由；只要求主知识总结读取开关开启。"""
    router = APIRouter(prefix="/api/v1", tags=["knowledge-summary-generations"])

    @router.get(
        "/conversations/{thread_id}/turns/{turn_id}/knowledge-summary-generation",
        response_model=CurrentTurnKnowledgeSummaryGenerationResponse,
    )
    async def get_current_turn_knowledge_summary_generation(
        thread_id: UUID,
        turn_id: UUID,
        auth: AuthContext = Depends(get_auth_context),
        ctx: ConversationApiContext = Depends(get_conversation_context),
    ) -> CurrentTurnKnowledgeSummaryGenerationResponse:
        """查询当前 Turn 应展示的最新非 cancelled Generation（§15.9）。"""
        return await service.get_current_generation_for_turn(
            user_id=auth.user_id,
            thread_id=thread_id,
            turn_id=turn_id,
        )

    @router.get(
        "/knowledge-summary-generations/{generation_id}",
        response_model=KnowledgeSummaryGenerationStatusResponse,
    )
    async def get_knowledge_summary_generation_status(
        generation_id: UUID,
        auth: AuthContext = Depends(get_auth_context),
        ctx: ConversationApiContext = Depends(get_conversation_context),
    ) -> KnowledgeSummaryGenerationStatusResponse:
        """读取单个 Generation Job 状态（§15.10）。"""
        return await service.get_generation_status(
            user_id=auth.user_id,
            generation_id=generation_id,
        )

    return router


def build_knowledge_summary_generation_router(
    service: KnowledgeSummaryGenerationApiService,
) -> APIRouter:
    """创建完整 Generation 路由；需要 generation 开关已开启。"""
    router = build_knowledge_summary_generation_read_router(service)

    @router.post(
        "/conversations/{thread_id}/turns/{turn_id}/knowledge-summary-generations",
        response_model=KnowledgeSummaryGenerationResponse,
        status_code=202,
    )
    async def create_knowledge_summary_generation(
        thread_id: UUID,
        turn_id: UUID,
        request: CreateKnowledgeSummaryGenerationRequest,
        fastapi_request: Request,
        auth: AuthContext = Depends(get_auth_context),
        ctx: ConversationApiContext = Depends(get_conversation_context),
    ) -> KnowledgeSummaryGenerationResponse:
        """手动触发/重试/刷新知识总结生成（§15.8）。"""
        return await service.ensure_manual_generation(
            user_id=auth.user_id,
            thread_id=thread_id,
            turn_id=turn_id,
            request=request,
            client_ip_address=_client_ip(fastapi_request, ctx),
        )

    @router.post(
        "/knowledge-summary-generations/{generation_id}/dismiss-review",
        status_code=204,
    )
    async def dismiss_knowledge_summary_review(
        generation_id: UUID,
        request: DismissReviewRequest,
        auth: AuthContext = Depends(get_auth_context),
        ctx: ConversationApiContext = Depends(get_conversation_context),
    ) -> None:
        """忽略一条待确认建议并重新计算 summary review_state（§15.11）。"""
        await service.dismiss_review(
            user_id=auth.user_id,
            generation_id=generation_id,
            review_id=request.review_id,
        )

    return router
