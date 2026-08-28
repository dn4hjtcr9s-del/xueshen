"""知识总结只读 HTTP API（知识总结方案 §15.1–§15.5）。

路由仅在 CONVERSATION_KNOWLEDGE_SUMMARY_ENABLED=true 时由 composition root 挂载；
生成、编辑和删除端点仍留在后续阶段，避免功能开关关闭态误暴露写路径。
"""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from backend.auth.context import AuthContext
from backend.conversation.contracts.knowledge_summary import (
    AllKnowledgeSection,
    KnowledgeSummaryDetailResponse,
    KnowledgeSummaryListResponse,
    KnowledgeSummaryPatchRequest,
    KnowledgeSummarySourcePage,
    KnowledgeSummaryStatsResponse,
    KnowledgeSummaryTopicGroupResponse,
)
from backend.conversation.services.knowledge_summary_service import KnowledgeSummaryService
from backend.memory.api.dependencies import get_auth_context


def build_knowledge_summaries_router(service: KnowledgeSummaryService) -> APIRouter:
    """创建知识总结只读路由；鉴权后的 user_id 只从 AuthContext 取得。"""
    router = APIRouter(prefix="/api/v1/knowledge-summaries", tags=["knowledge-summaries"])

    @router.get("", response_model=KnowledgeSummaryListResponse)
    async def list_knowledge_summaries(
        query: Annotated[str | None, Query(max_length=200)] = None,
        topic_group: Annotated[str | None, Query(max_length=160)] = None,
        section_type: Annotated[list[AllKnowledgeSection] | None, Query()] = None,
        review_state: Annotated[
            Literal["clean", "possible_duplicate", "conflict"] | None, Query()
        ] = None,
        sort: Annotated[
            Literal["relevance_desc", "updated_desc", "title_asc"] | None, Query()
        ] = None,
        cursor: Annotated[str | None, Query(max_length=2000)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        auth: AuthContext = Depends(get_auth_context),
    ) -> KnowledgeSummaryListResponse:
        """列出当前用户知识总结，支持冻结的搜索、筛选和 keyset 分页。"""
        return await service.list_summaries(
            user_id=auth.user_id,
            query=query,
            topic_group=topic_group,
            section_types=section_type or [],
            review_state=review_state,
            sort=sort,
            cursor=cursor,
            limit=limit,
        )

    @router.get("/topic-groups", response_model=KnowledgeSummaryTopicGroupResponse)
    async def list_knowledge_summary_topic_groups(
        query: Annotated[str | None, Query(max_length=200)] = None,
        cursor: Annotated[str | None, Query(max_length=2000)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 50,
        auth: AuthContext = Depends(get_auth_context),
    ) -> KnowledgeSummaryTopicGroupResponse:
        """列出当前用户可用于筛选的非空大主题。"""
        return await service.list_topic_groups(
            user_id=auth.user_id,
            query=query,
            cursor=cursor,
            limit=limit,
        )

    @router.get("/stats", response_model=KnowledgeSummaryStatsResponse)
    async def get_knowledge_summary_stats(
        auth: AuthContext = Depends(get_auth_context),
    ) -> KnowledgeSummaryStatsResponse:
        """返回首页和个人中心使用的知识总结统计。"""
        return await service.get_stats(user_id=auth.user_id)

    @router.get("/{summary_id}", response_model=KnowledgeSummaryDetailResponse)
    async def get_knowledge_summary(
        summary_id: UUID,
        auth: AuthContext = Depends(get_auth_context),
    ) -> KnowledgeSummaryDetailResponse:
        """读取一张 active 总结及其结构化评审状态。"""
        return await service.get_summary_detail(user_id=auth.user_id, summary_id=summary_id)

    @router.patch("/{summary_id}", response_model=KnowledgeSummaryDetailResponse)
    async def patch_knowledge_summary(
        summary_id: UUID,
        request: KnowledgeSummaryPatchRequest,
        auth: AuthContext = Depends(get_auth_context),
    ) -> KnowledgeSummaryDetailResponse:
        """按 expected_version 原子提交用户编辑和章节保护状态。"""
        return await service.patch_summary(
            user_id=auth.user_id,
            summary_id=summary_id,
            request=request,
        )

    @router.delete("/{summary_id}", status_code=204, response_class=Response)
    async def delete_knowledge_summary(
        summary_id: UUID,
        expected_version: Annotated[int, Query(ge=1)],
        auth: AuthContext = Depends(get_auth_context),
    ) -> Response:
        """按 §15.7 执行带 tombstone 的幂等软删除。"""
        await service.delete_summary(
            user_id=auth.user_id,
            summary_id=summary_id,
            expected_version=expected_version,
        )
        return Response(status_code=204)

    @router.get("/{summary_id}/sources", response_model=KnowledgeSummarySourcePage)
    async def list_knowledge_summary_sources(
        summary_id: UUID,
        cursor: Annotated[str | None, Query(max_length=2000)] = None,
        limit: Annotated[int, Query(ge=1, le=50)] = 20,
        auth: AuthContext = Depends(get_auth_context),
    ) -> KnowledgeSummarySourcePage:
        """按 Turn 聚合来源卡，返回同序支撑消息而不公开内部 source_id。"""
        return await service.list_source_turns(
            user_id=auth.user_id,
            summary_id=summary_id,
            cursor=cursor,
            limit=limit,
        )

    return router
