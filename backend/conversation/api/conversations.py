"""Conversation REST API：创建/列表/详情/删除会话（方案 §17.1）。

- cursor 分页：会话列表 (updated_at DESC, thread_id DESC)，不透明 cursor；
- 删除中/已删除线程默认不进入列表；
- 挂载到现有 FastAPI App，复用统一认证依赖。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from backend.auth.context import AuthContext
from backend.conversation.api.dependencies import (
    ConversationApiContext,
    get_conversation_context,
)
from backend.conversation.contracts.api import (
    ConversationDetailResponse,
    ConversationListResponse,
    CreateConversationResponse,
    ThreadListItem,
)
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.services.conversation_service import ConversationService
from backend.memory.api.dependencies import get_auth_context


def build_conversations_router(
    service: ConversationService,
) -> APIRouter:
    """会话路由（composition root 注入 service）。"""

    router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])

    @router.post("", response_model=CreateConversationResponse, status_code=201)
    async def create_conversation(
        auth: AuthContext = Depends(get_auth_context),
        ctx: ConversationApiContext = Depends(get_conversation_context),
    ) -> Any:
        """创建会话（§17.1）。"""
        return await service.create_thread(user_id=auth.user_id)

    @router.get("", response_model=ConversationListResponse)
    async def list_conversations(
        cursor: str | None = Query(default=None, max_length=500),
        limit: int = Query(default=50, ge=1, le=100),
        auth: AuthContext = Depends(get_auth_context),
        ctx: ConversationApiContext = Depends(get_conversation_context),
    ) -> Any:
        """会话列表（§17.1 cursor 分页：updated_at DESC, thread_id DESC）。"""
        before = ctx.decode_cursor(cursor) if cursor else None
        async with ctx.session_factory() as session:
            # P1-2（评审）：limit+1 探测 has_more（LIMIT :limit 下恒有 len<=limit）
            rows = await threads_repo.list_threads(
                session, auth.user_id, before_cursor=before, limit=limit + 1
            )
            has_more = len(rows) > limit
            items_rows = rows[:limit]
            items = [
                ThreadListItem(
                    thread_id=row["thread_id"],
                    title=row["title"] or "",
                    status=row["status"],
                    version=row["version"],
                    updated_at=row["updated_at"],
                )
                for row in items_rows
            ]
            next_cursor = None
            if items_rows and has_more:
                last = items_rows[-1]
                next_cursor = ctx.encode_cursor(last["updated_at"], last["thread_id"])
        return ConversationListResponse(items=items, next_cursor=next_cursor, has_more=has_more)

    @router.get("/{thread_id}", response_model=ConversationDetailResponse)
    async def get_conversation_detail(
        thread_id: UUID,
        before_sequence: int | None = Query(default=None, ge=1),
        limit: int = Query(default=50, ge=1, le=100),
        auth: AuthContext = Depends(get_auth_context),
        ctx: ConversationApiContext = Depends(get_conversation_context),
    ) -> Any:
        """会话详情与分页消息（§17.1：before_sequence 向历史方向，响应正序）。"""
        from backend.conversation.contracts.api import ConversationDetailResponse, MessageView
        from backend.conversation.contracts.errors import ConversationNotFoundError
        from backend.conversation.persistence import messages as messages_repo

        async with ctx.session_factory() as session:
            thread = await threads_repo.get_thread(session, thread_id)
            if thread is None or thread["user_id"] != auth.user_id:
                raise ConversationNotFoundError("会话不存在或无权访问")
            rows = await messages_repo.list_messages(
                session, thread_id, before_sequence=before_sequence, limit=limit + 1
            )
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            page_rows.reverse()  # 数据库倒序取页后按 sequence 正序返回（§17.1）
            messages = [
                MessageView(
                    message_id=row["message_id"],
                    thread_id=row["thread_id"],
                    turn_id=row["turn_id"],
                    role=row["role"],
                    content=row["content"],
                    status=row["status"],
                    sequence=row["sequence"],
                    occurred_at=row["occurred_at"],
                    completed_at=row.get("completed_at"),
                )
                for row in page_rows
            ]
        return ConversationDetailResponse(
            thread_id=thread_id,
            title=thread["title"] or "",
            version=thread["version"],
            status=thread["status"],
            messages=messages,
            # P1-2（评审）：next_cursor 取页尾（最旧一条）而非页首，翻页不再原地循环
            next_cursor=str(rows[limit - 1]["sequence"]) if has_more else None,
            has_more=has_more,
        )

    @router.delete("/{thread_id}", status_code=202)
    async def delete_conversation(
        thread_id: UUID,
        auth: AuthContext = Depends(get_auth_context),
        ctx: ConversationApiContext = Depends(get_conversation_context),
    ) -> dict[str, str]:
        """删除会话（§8.6 步骤 1）：事务置 deleting，触发可靠本地清理和 source deletion。"""
        await ctx.service.delete_thread(user_id=auth.user_id, thread_id=thread_id)
        return {"status": "deleting"}

    return router
