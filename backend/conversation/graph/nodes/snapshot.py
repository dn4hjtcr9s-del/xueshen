"""build_turn_snapshot 节点（方案 §5.2 / §9）。

构造不可变 TurnContextSnapshot；Rewrite 与 Answer 必须使用同一快照（§9.1）。
"""

from __future__ import annotations

from typing import Any

from backend.conversation.graph.state import ConversationRuntimeContext, serialize_snapshot


async def build_turn_snapshot(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
    context_service: Any,
) -> dict[str, Any]:
    """构造快照并写入 State（snapshot/snapshot_hash）。"""
    conversation_context = state.get("conversation_context") or {}
    memory_context = state.get("memory_context") or {}
    snapshot = context_service.build_snapshot(
        user_id=state["user_id"],
        thread_id=state["thread_id"],
        turn_id=state["turn_id"],
        current_message=str(conversation_context.get("current_message") or ""),
        recent_messages=conversation_context.get("recent_messages") or [],
        conversation_summary=conversation_context.get("conversation_summary"),
        memory=memory_context if memory_context.get("status") != "unavailable" else None,
        memory_status=str(memory_context.get("status") or "unavailable"),
    )
    return {
        "snapshot": serialize_snapshot(snapshot),
        "snapshot_hash": snapshot.context_hash,
    }
