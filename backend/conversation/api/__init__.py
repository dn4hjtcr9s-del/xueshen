"""Conversation API 包：Router 组装（方案 §17）。

build_conversation_routers(app) 在 FastAPI 运行时装配：
- 需要 ConversationDatabase + ConversationService + TurnEventWriter + 限流器；
- 把 ConversationApiContext 挂到 app.state.conversation_api_context，
  供 get_conversation_context 依赖使用。
"""

from __future__ import annotations

from fastapi import APIRouter, FastAPI

from backend.conversation.api.conversations import build_conversations_router
from backend.conversation.api.dependencies import build_conversation_api_context
from backend.conversation.api.events import build_events_router
from backend.conversation.api.knowledge_summaries import build_knowledge_summaries_router
from backend.conversation.api.knowledge_summary_generations import (
    build_knowledge_summary_generation_read_router,
    build_knowledge_summary_generation_router,
)
from backend.conversation.api.turns import build_turns_router
from backend.conversation.persistence.database import ConversationDatabase
from backend.conversation.persistence.event_writer import TurnEventWriter
from backend.conversation.services.conversation_service import ConversationService
from backend.conversation.services.knowledge_summary_generation_api import (
    KnowledgeSummaryGenerationApiService,
)
from backend.conversation.services.knowledge_summary_service import KnowledgeSummaryService
from backend.memory.api.dependencies import FixedWindowRateLimiter


def build_conversation_routers(app: FastAPI) -> APIRouter | None:
    """按 app.state.settings 装配 Conversation Router（未启用时返回 None）。

    create_app 可注入测试或运维专用 Settings；Conversation 的 Feature Flag 必须与
    app 实际持有的配置一致，不能回退读取全局 get_settings()。
    """
    settings = app.state.settings
    if not settings.conversation_database_url:
        return None
    from backend.conversation.graph.state import SystemIdGenerator

    db = ConversationDatabase(settings)
    writer = TurnEventWriter(id_generator=SystemIdGenerator())
    service = ConversationService(
        session_factory=db.session_factory,
        turn_event_writer=writer,
    )
    ctx = build_conversation_api_context(
        settings=settings,
        session_factory=db.session_factory,
        service=service,
        rate_limiter=FixedWindowRateLimiter(),
    )
    app.state.conversation_api_context = ctx
    app.state.conversation_db = db
    router = APIRouter()
    router.include_router(build_conversations_router(service))
    router.include_router(build_turns_router())
    router.include_router(build_events_router())
    # 主读取开关关闭时完全不挂载知识总结用户路由（方案 §15 / §19）。
    if settings.conversation_knowledge_summary_enabled:
        knowledge_summary_service = KnowledgeSummaryService(
            session_factory=db.session_factory,
            settings=settings,
        )
        router.include_router(build_knowledge_summaries_router(knowledge_summary_service))
        generation_service = KnowledgeSummaryGenerationApiService(
            session_factory=db.session_factory,
            settings=settings,
            rate_limiter=FixedWindowRateLimiter(),
        )
        # generation 关闭时保留历史 Generation 只读查询，不挂载创建/重试/dismiss 写路径。
        if settings.conversation_knowledge_summary_generation_enabled:
            router.include_router(build_knowledge_summary_generation_router(generation_service))
        else:
            router.include_router(
                build_knowledge_summary_generation_read_router(generation_service)
            )
    return router
