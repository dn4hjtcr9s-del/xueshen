"""Conversation SSE 事件流（方案 §17.4/§17.5/§1.5 R1）。

- Fetch SSE：Authorization Bearer Header + Accept: text/event-stream + Last-Event-ID；
  不使用原生 EventSource，URL 不携带 token/ticket（§17.5 #1/#2）；
- 服务端从 conversation_turn_events 重放后再切实时流（§17.5 #3）；
- Last-Event-ID 早于最早保留事件 → HTTP 410 EVENT_REPLAY_EXPIRED（R1）；
- 事件按 event_id/sequence 去重由前端负责；断线默认不取消 Graph（§17.5 #5）；
- 心跳：CONVERSATION_SSE_HEARTBEAT_SECONDS。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from backend.auth.context import AuthContext
from backend.conversation.api.dependencies import (
    ConversationApiContext,
    get_conversation_context,
)
from backend.conversation.contracts.errors import (
    EventReplayExpiredError,
    TurnNotFoundError,
)
from backend.conversation.persistence import turns as turns_repo
from backend.memory.api.dependencies import get_auth_context


def build_events_router() -> APIRouter:
    """SSE 事件流路由。"""

    router = APIRouter(prefix="/api/v1/conversations", tags=["conversation-events"])

    @router.get("/{thread_id}/turns/{turn_id}/events")
    async def stream_turn_events(
        thread_id: UUID,
        turn_id: UUID,
        request: Request,
        auth: AuthContext = Depends(get_auth_context),
        ctx: ConversationApiContext = Depends(get_conversation_context),
    ) -> StreamingResponse:
        """SSE 流与重放（§17.5）。"""
        async with ctx.session_factory() as session:
            row = await turns_repo.get_turn(session, turn_id)
            if row is None or row["thread_id"] != thread_id or row["user_id"] != auth.user_id:
                raise TurnNotFoundError("Turn 不存在或无权访问")
            earliest = await turns_repo.earliest_event_sequence(session, turn_id)

        last_event_id = _parse_last_event_id(request.headers.get("last-event-id"))
        if last_event_id is not None and earliest is not None and last_event_id < earliest:
            raise EventReplayExpiredError("事件流已过期，请重新拉取 Turn/Thread 后订阅")

        request_id = getattr(request.state, "trace_id", None) or ""
        return StreamingResponse(
            _event_stream(
                ctx=ctx,
                turn_id=turn_id,
                thread_id=thread_id,
                run_id=row["run_id"],
                request_id=request_id,
                resume_after=last_event_id,
                heartbeat_seconds=ctx.settings.conversation_sse_heartbeat_seconds,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # 反向代理关闭 SSE 缓冲（§17.5 #8）
            },
        )

    return router


def _parse_last_event_id(raw: str | None) -> int | None:
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


async def _event_stream(
    *,
    ctx: ConversationApiContext,
    turn_id: UUID,
    thread_id: UUID,
    run_id: str,
    request_id: str,
    resume_after: int | None,
    heartbeat_seconds: int,
) -> Any:
    """SSE 生成器：重放 → 轮询新事件 → 心跳。"""
    last_sequence = resume_after or 0
    # 1. 重放（§17.5 #3）
    async with ctx.session_factory() as session:
        events = await turns_repo.list_events(session, turn_id)
    for event in events:
        if event["sequence"] <= last_sequence:
            continue
        yield _format_sse(event, thread_id=thread_id)
        last_sequence = int(event["sequence"])

    # 2. 实时轮询新事件（1s 间隔，与心跳无关）+ 心跳（15s）
    poll_interval = 1.0
    loop = asyncio.get_running_loop()
    last_heartbeat = loop.time()
    while True:
        async with ctx.session_factory() as session:
            new_events = [
                e
                for e in await turns_repo.list_events(session, turn_id)
                if int(e["sequence"]) > last_sequence
            ]
        for event in new_events:
            yield _format_sse(event, thread_id=thread_id)
            last_sequence = int(event["sequence"])
        if last_sequence and await _turn_terminal(ctx, turn_id):
            # 终态事件已全部送达后结束流（评审 P1-1：客户端不再无限重连）
            break
        now = loop.time()
        if now - last_heartbeat >= heartbeat_seconds:
            yield ": heartbeat\n\n"
            last_heartbeat = now
        await asyncio.sleep(poll_interval)


async def _turn_terminal(ctx: ConversationApiContext, turn_id: UUID) -> bool:
    """终态（completed/failed/cancelled）后流可结束。"""
    from backend.conversation.persistence import turns as turns_repo

    async with ctx.session_factory() as session:
        row = await turns_repo.get_turn(session, turn_id)
    return row is not None and row["status"] in ("completed", "failed", "cancelled", "deleted")


def _format_sse(event: dict[str, Any], thread_id: UUID | None = None) -> str:
    """SSE 事件序列化（§17.4 envelope）。

    修复（评审 P1-1）：thread_id 与 turn_id 分列——此前两者都填了 turn_id。
    """
    payload = event["payload"] or {}
    envelope = {
        "schema_version": "1",
        "event_id": str(event["sequence"]),
        "sequence": int(event["sequence"]),
        "event_type": event["event_type"],
        "request_id": event["request_id"],
        "thread_id": str(thread_id or event["turn_id"]),
        "turn_id": str(event["turn_id"]),
        "run_id": event["run_id"],
        "occurred_at": event["occurred_at"].isoformat(),
        "data": payload,
    }
    data = json.dumps(envelope, ensure_ascii=False)
    return f"id: {event['sequence']}\nevent: {event['event_type']}\ndata: {data}\n\n"
