"""Conversation Turn API：创建/查询/取消（方案 §17.1/§17.2/§17.3/§1.5 R2）。

- 创建：幂等（client_request_id）+ version 乐观锁 + 同线程串行 + 限流；
- 取消（R2）：accepted 直接转 cancelled；running 转 cancelling；
  已终态取消请求幂等返回，不重复写事件；
- run_id 由 worker claim 后生成，此处用 request_id 占位。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from backend.auth.context import AuthContext
from backend.conversation.api.dependencies import (
    ConversationApiContext,
    get_conversation_context,
)
from backend.conversation.contracts.api import (
    CreateTurnRequest,
    CreateTurnResponse,
    TurnStatusResponse,
)
from backend.conversation.contracts.errors import TurnNotFoundError
from backend.conversation.contracts.events import TurnEventWrite
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo
from backend.memory.api.dependencies import get_auth_context


def build_turns_router() -> APIRouter:
    """Turn 路由（composition root 装配 service 上下文）。"""

    router = APIRouter(prefix="/api/v1/conversations", tags=["conversation-turns"])

    @router.post("/{thread_id}/turns", response_model=CreateTurnResponse, status_code=202)
    async def create_turn(
        thread_id: UUID,
        request_body: CreateTurnRequest,
        request: Request,
        auth: AuthContext = Depends(get_auth_context),
        ctx: ConversationApiContext = Depends(get_conversation_context),
    ) -> Any:
        """幂等创建 Turn（§17.2/§17.3）；限流默认 10 次/分钟/用户（Q13/评审 P1-3）。

        - 限流值取配置 CONVERSATION_TURN_RATE_LIMIT_PER_MINUTE（不硬编码）；
        - 幂等命中不重复计数（Q13）：先做只读幂等预检，命中则直接返回不计数。
        """
        # 幂等预检（只读，不计数）：命中已有 Turn 直接返回（Q13）
        existing = await ctx.service.find_turn_by_client_request(
            user_id=auth.user_id,
            thread_id=thread_id,
            client_request_id=request_body.client_request_id,
        )
        if existing is not None:
            return CreateTurnResponse(
                thread_id=existing["thread_id"],
                turn_id=existing["turn_id"],
                user_message_id=existing["user_message_id"],
                thread_version=existing["thread_version"],
                status=existing["status"],
                event_stream_path=(
                    f"/api/v1/conversations/{existing['thread_id']}/turns/"
                    f"{existing['turn_id']}/events"
                ),
            )
        limiter: Any = ctx.rate_limiter
        limit = ctx.settings.conversation_turn_rate_limit_per_minute
        if not limiter.hit("conversation_turn", str(auth.user_id), limit):
            from backend.memory.contracts.errors import RateLimitedError

            raise RateLimitedError("请求超过限流阈值，请稍后重试")
        result = await ctx.service.create_turn(
            user_id=auth.user_id,
            thread_id=thread_id,
            client_request_id=request_body.client_request_id,
            content=request_body.content,
            expected_thread_version=request_body.expected_thread_version,
            request_id=getattr(request.state, "trace_id", None) or "",
            run_id=getattr(request.state, "trace_id", None) or "",
        )
        return CreateTurnResponse(
            thread_id=result["thread_id"],
            turn_id=result["turn_id"],
            user_message_id=result["user_message_id"],
            thread_version=result["thread_version"],
            status=result["status"],
            event_stream_path=(
                f"/api/v1/conversations/{result['thread_id']}/turns/{result['turn_id']}/events"
            ),
        )

    @router.get("/{thread_id}/turns/{turn_id}", response_model=TurnStatusResponse)
    async def get_turn_status(
        thread_id: UUID,
        turn_id: UUID,
        auth: AuthContext = Depends(get_auth_context),
        ctx: ConversationApiContext = Depends(get_conversation_context),
    ) -> Any:
        """查询 Turn 当前状态（§17.1）。"""
        async with ctx.session_factory() as session:
            row = await turns_repo.get_turn(session, turn_id)
            if row is None or row["thread_id"] != thread_id or row["user_id"] != auth.user_id:
                raise TurnNotFoundError("Turn 不存在或无权访问")
            thread = await threads_repo.get_thread(session, thread_id)
            thread_version = int(thread["version"]) if thread else 0
        return TurnStatusResponse(
            turn_id=row["turn_id"],
            thread_id=row["thread_id"],
            status=row["status"],
            thread_version=thread_version,
            assistant_message_id=row.get("assistant_message_id"),
            event_stream_path=(f"/api/v1/conversations/{thread_id}/turns/{turn_id}/events"),
        )

    @router.delete("/{thread_id}/turns/{turn_id}", status_code=202)
    async def cancel_turn(
        thread_id: UUID,
        turn_id: UUID,
        request: Request,
        auth: AuthContext = Depends(get_auth_context),
        ctx: ConversationApiContext = Depends(get_conversation_context),
    ) -> dict[str, str]:
        """请求取消生成（R2：accepted 直接转 cancelled；running 转 cancelling）。"""
        async with ctx.session_factory() as session:
            async with session.begin():
                row = await turns_repo.get_turn(session, turn_id)
                if row is None or row["thread_id"] != thread_id or row["user_id"] != auth.user_id:
                    raise TurnNotFoundError("Turn 不存在或无权访问")
                if row["status"] == "accepted":
                    await turns_repo.cancel_accepted_turn(session, turn_id)
                    await ctx.service.turn_event_writer.append(
                        session,
                        write=TurnEventWrite(
                            turn_id=turn_id,
                            event_type="turn.cancelled",
                            request_id=getattr(request.state, "trace_id", None) or "",
                            run_id=row["run_id"],
                            payload={
                                "status": "cancelled",
                                "partial_answer_available": False,
                            },
                        ),
                    )
                    return {"status": "cancelled"}
                if row["status"] == "running":
                    await turns_repo.mark_cancelling(session, turn_id)
                    return {"status": "cancelling"}
                # 已终态：幂等返回当前状态，不重复写事件（R2）
                return {"status": row["status"]}

    return router
