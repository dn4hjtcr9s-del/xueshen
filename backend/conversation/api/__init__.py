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
from backend.conversation.api.turns import build_turns_router
from backend.conversation.persistence.database import ConversationDatabase
from backend.conversation.persistence.event_writer import TurnEventWriter
from backend.conversation.services.conversation_service import ConversationService
from backend.memory.api.dependencies import FixedWindowRateLimiter


def build_conversation_routers(app: FastAPI) -> APIRouter | None:
    """按当前 Settings 装配 Conversation Router（未启用时返回 None）。"""
    from backend.settings import get_settings

    settings = get_settings()
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
    return router
