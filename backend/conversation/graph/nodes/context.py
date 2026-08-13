"""load_conversation_context 节点（方案 §5.2 / §9.3 #1）。

从 Conversation 库读取：当前消息、有界最近消息、历史摘要；
返回可序列化 conversation_context dict（进入 Graph State）。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


async def load_conversation_context(
    state: dict[str, Any],
    *,
    session_factory: async_sessionmaker[AsyncSession],
    max_messages: int,
) -> dict[str, Any]:
    """读取最近消息与最新摘要（§9.3 #1）。"""
    if session_factory is None:
        # 单测无 DB：使用 state 直注入的上下文（由测试预先设置 snapshot/上下文）
        snapshot = state.get("snapshot") or {}
        return {
            "current_message": str(snapshot.get("current_message") or ""),
            "recent_messages": snapshot.get("recent_messages") or [],
            "conversation_summary": snapshot.get("conversation_summary"),
        }
    thread_id = state["thread_id"]
    current_message_id = state.get("user_message_id")
    async with session_factory() as session:
        messages = await _recent_messages(session, thread_id, limit=max_messages)
        summary = await _latest_summary(session, thread_id)
        current = next(
            (m for m in messages if str(m["message_id"]) == str(current_message_id)), None
        )
        if current is None:
            current = await _get_message(session, UUID(str(current_message_id)))
    # P2（评审 / 附录 A.5）：当前用户消息不占 20 条计数、不重复出现在
    # recent_messages（历史截断顺序规则 1：当前消息永远完整保留）。
    recent = [
        {
            "message_id": str(m["message_id"]),
            "role": m["role"],
            "sequence": int(m["sequence"]),
            "content": m["content"],
        }
        for m in messages
        if str(m["message_id"]) != str(current_message_id)
    ]
    return {
        "current_message": str(current["content"]) if current else "",
        "recent_messages": recent,
        "conversation_summary": summary,
    }


async def _recent_messages(
    session: AsyncSession, thread_id: UUID, *, limit: int
) -> list[dict[str, Any]]:
    result = await session.execute(
        text(
            "SELECT message_id, role, sequence, content "
            "FROM conversation.conversation_messages "
            "WHERE thread_id = :thread_id AND status = 'completed' "
            "  AND eligible_for_context = true "
            "ORDER BY sequence DESC LIMIT :limit"
        ),
        {"thread_id": thread_id, "limit": limit},
    )
    rows = [dict(r) for r in result.mappings()]
    rows.reverse()
    return rows


async def _latest_summary(session: AsyncSession, thread_id: UUID) -> str | None:
    result = await session.execute(
        text(
            "SELECT content FROM conversation.conversation_summaries "
            "WHERE thread_id = :thread_id ORDER BY sequence DESC LIMIT 1"
        ),
        {"thread_id": thread_id},
    )
    row = result.mappings().first()
    return str(row["content"]) if row else None


async def _get_message(session: AsyncSession, message_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            "SELECT message_id, role, sequence, content "
            "FROM conversation.conversation_messages "
            "WHERE message_id = :message_id AND status = 'completed'"
        ),
        {"message_id": message_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None
