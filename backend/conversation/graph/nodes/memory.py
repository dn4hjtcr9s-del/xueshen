"""recall_memory 节点（方案 §5.2 / §9.3 #2 / §16）。

本轮唯一一次 Memory 读取；query seed = 当前问题 + 最近用户消息摘要。
Memory 成功结果与失败状态都写入快照；补检索循环不得再次调用（§9.3 #6）。

失败分类（§16.2 / 评审 P1-5）：
- 认证/权限（401/403）→ 抛错使 Turn 失败，不静默降级；
- 超时/5xx/网络 → unavailable 快照继续本轮对话；
- 4xx 契约错误 → 抛错使 Turn 失败并告警，禁止当普通降级处理。
"""

from __future__ import annotations

from typing import Any

from backend.conversation.contracts.errors import MemoryUnavailableError
from backend.conversation.graph.state import ConversationRuntimeContext


async def recall_memory(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
) -> dict[str, Any]:
    """读取长期记忆（§16.1）。

    返回 {"memory_context": {...}} 供 build_turn_snapshot 使用；
    MEMORY_READ_ENABLED=false 时短路为 unavailable（附录 A.10）。
    """
    flags = runtime.flags
    if not flags.get("memory_read", True):
        return {"memory_context": {"status": "unavailable"}}
    query_seed = _build_query_seed(state)
    try:
        context = await runtime.memory_gateway.build_learning_context(
            query=query_seed,
            token_budget=None,  # 预算在快照构建后由 ContextService 固化（§9.2）
            user_id=str(state["user_id"]),
        )
    except MemoryUnavailableError as exc:
        # 第三轮必改 4：401/403 与 4xx 契约错误必须 Turn 失败，不静默降级
        # （gateway 透传 source_http_status；无状态按 5xx/降级处理）。
        source_status = exc.source_http_status
        if source_status is not None and source_status < 500:
            runtime.logger.warning("Memory 读取被拒绝（不可降级）: http=%s", source_status)
            raise
        # 5xx/超时/网络（或未知）：unavailable 快照继续本轮对话（§16.2）
        await _emit_degraded(runtime, state, "memory_unavailable")
        return {"memory_context": {"status": "unavailable"}}
    except Exception:
        # 未知异常：unavailable 继续（§16.2）
        await _emit_degraded(runtime, state, "memory_unavailable")
        return {"memory_context": {"status": "unavailable"}}
    memory_status = "degraded" if context.get("truncated") else "available"
    if memory_status == "degraded":
        await _emit_degraded(runtime, state, "memory_degraded")
    context["status"] = memory_status
    return {"memory_context": context}


async def _emit_degraded(
    runtime: ConversationRuntimeContext,
    state: dict[str, Any],
    flag: str,
) -> None:
    """§17.4.1：turn.degraded 事件（第三轮评审 P2：发射点补齐）。

    Memory 降级（unavailable/degraded）时通知前端展示降级状态；
    与检索降级（retrieval_partial/unavailable）共用同一事件类型。
    """
    from backend.conversation.contracts.events import TurnEventWrite

    repo = runtime.conversation_repository
    if repo is None or repo.session_factory is None:
        return
    async with repo.session_factory() as session:
        async with session.begin():
            await runtime.turn_event_writer.append(
                session,
                write=TurnEventWrite(
                    turn_id=state["turn_id"],
                    event_type="turn.degraded",
                    request_id=str(state.get("request_id") or ""),
                    run_id=str(state.get("run_id") or ""),
                    payload={"flags": [flag]},
                ),
            )


def _build_query_seed(state: dict[str, Any]) -> str:
    """query seed：当前问题 + 最近用户消息摘要（§9.3 #2）。"""
    snapshot = state.get("snapshot") or {}
    current = str(
        snapshot.get("current_message")
        or state.get("conversation_context", {}).get("current_message")
        or ""
    )
    recent = state.get("conversation_context", {}).get("recent_messages") or []
    user_parts = [str(m["content"]) for m in recent if m.get("role") == "user"]
    seed = current
    if user_parts:
        seed = f"{seed} {user_parts[-1]}"
    return seed[:500] or "最近对话"
