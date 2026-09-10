"""build_turn_snapshot 节点（方案 §5.2 / §9）。

构造不可变 TurnContextSnapshot；Rewrite 与 Answer 必须使用同一快照（§9.1）。
"""

from __future__ import annotations

from typing import Any

from backend.conversation.graph.state import ConversationRuntimeContext, serialize_snapshot
from backend.conversation.rollout.recorder import record_rollout


async def build_turn_snapshot(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
    context_service: Any,
) -> dict[str, Any]:
    """构造快照并写入 State（snapshot/snapshot_hash）。"""
    conversation_context = state.get("conversation_context") or {}
    memory_context = state.get("memory_context") or {}
    memory_status = str(memory_context.get("status") or "unavailable")
    snapshot = context_service.build_snapshot(
        user_id=state["user_id"],
        thread_id=state["thread_id"],
        turn_id=state["turn_id"],
        current_message=str(conversation_context.get("current_message") or ""),
        recent_messages=conversation_context.get("recent_messages") or [],
        conversation_summary=conversation_context.get("conversation_summary"),
        memory=memory_context if memory_context.get("status") != "unavailable" else None,
        memory_status=memory_status,
    )
    # memory-rebuild §5.3 写入顺序第 3 条：turn_context_snapshot 只落摘要级
    # （hash + 消息 ID 序列 + token 数 + memory_status），正文不重复落入 rollout。
    await record_rollout(
        runtime,
        "turn_context_snapshot",
        {
            "snapshot_hash": snapshot.context_hash,
            "message_ids": [str(message.message_id) for message in snapshot.recent_messages],
            "token_estimate": snapshot.budgets.history_tokens + snapshot.budgets.memory_tokens,
            "memory_status": memory_status,
        },
    )
    return {
        "snapshot": serialize_snapshot(snapshot),
        "snapshot_hash": snapshot.context_hash,
    }
