"""explicit_remember_ack 节点（方案 §16.4 / Q3）。

显式记忆请求的低延迟接收确认由 Graph 内 MEMORYACK 节点执行，不是 Graph 外
Worker hook（§5.4）。该节点只能按与 Publisher 相同的 Outbox lease、generation、
fencing 和幂等规则快速 claim 当前 Turn 的 Outbox 行；claim 失败表示 Publisher
已持有，节点不得绕过 Outbox 并发直投（§5.4 / §16.4 #4）。

MEMORYACK 使用短超时投递；失败或超时仍保留 Outbox 供独立 Publisher 重试，
不在 finalize 数据库事务内调用 Memory（§5.4）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.conversation.contracts.events import TurnEventWrite
from backend.conversation.graph.state import ConversationRuntimeContext


async def explicit_remember_ack(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
) -> dict[str, Any]:
    """快速 claim 当前 Turn 的 memory Outbox 并短超时投递（§16.4）。"""
    turn_id = state["turn_id"]
    repo = runtime.conversation_repository
    event_id = state.get("outbox_event_id")
    if event_id is None:
        return {"memory_ack": {"status": "skipped"}}

    # 按 Publisher 同规则快速 claim（同一事务内 SKIP LOCKED）
    async with repo.session_factory() as session:
        async with session.begin():
            claimed = await _claim_current_outbox(
                session, turn_id=str(turn_id), worker_id=runtime.worker_id or "memoryack"
            )
            if claimed is None:
                # Publisher 已持有：不绕过 Outbox 并发直投（§16.4 #4）
                return {"memory_ack": {"status": "claimed_by_publisher"}}
            row = claimed
            try:
                result = await runtime.memory_gateway.submit_conversation_evidence(
                    idempotency_key=row["idempotency_key"],
                    thread_id=str(row["thread_id"]),
                    message_ids=[str(mid) for mid in row["message_ids"]],
                    trigger=row["trigger"] or "turn_boundary",
                    checkpoint_id=row["source_checkpoint_id"],
                    topic_hints=list(row["topic_hints"]),
                    graph_node_hints=list(row["graph_node_hints"]),
                )
            except Exception:
                # 失败保留 Outbox 供 Publisher 重试（§5.4）
                await session.execute(
                    text(
                        "UPDATE conversation.conversation_outbox "
                        "SET status = 'pending', lease_owner = NULL, lease_expires_at = NULL "
                        "WHERE event_id = :event_id AND lease_owner = :owner"
                    ),
                    {"event_id": row["event_id"], "owner": row["lease_owner"]},
                )
                return {"memory_ack": {"status": "failed_will_retry"}}
            operation_id = getattr(result, "operation_id", None)
            await session.execute(
                text(
                    "UPDATE conversation.conversation_outbox "
                    "SET status = 'delivered', delivered_at = :now, "
                    "    lease_owner = NULL, lease_expires_at = NULL "
                    "WHERE event_id = :event_id AND lease_owner = :owner"
                ),
                {
                    "event_id": row["event_id"],
                    "owner": row["lease_owner"],
                    "now": datetime.now(UTC),
                },
            )
            await session.execute(
                text(
                    "UPDATE conversation.conversation_turns "
                    "SET memory_submission_status = 'accepted', "
                    "    memory_operation_id = :op_id, updated_at = :now "
                    "WHERE turn_id = :turn_id"
                ),
                {
                    "op_id": operation_id,
                    "turn_id": turn_id,
                    "now": datetime.now(UTC),
                },
            )
            await runtime.turn_event_writer.append(
                session,
                write=TurnEventWrite(
                    turn_id=turn_id,
                    event_type="memory.submission",
                    request_id=str(state.get("request_id") or ""),
                    run_id=str(state.get("run_id") or ""),
                    payload={
                        "status": "accepted",
                        "operation_id": str(operation_id) if operation_id else None,
                    },
                ),
            )
    return {"memory_ack": {"status": "accepted"}}


async def _claim_current_outbox(
    session: AsyncSession,
    *,
    turn_id: str,
    worker_id: str,
) -> dict[str, Any] | None:
    """按 Publisher 同规则 claim 当前 Turn 的 memory Outbox（§5.4 / §16.4）。"""
    result = await session.execute(
        text(
            "SELECT * FROM conversation.conversation_outbox "
            "WHERE turn_id = CAST(:turn_id AS uuid) "
            "  AND event_type = 'conversation_evidence' "
            "  AND status IN ('pending', 'retry_wait') "
            "ORDER BY created_at LIMIT 1 "
            "FOR UPDATE SKIP LOCKED"
        ),
        {"turn_id": turn_id},
    )
    row = result.mappings().first()
    if row is None:
        return None
    row_dict = dict(row)
    generation = int(row_dict["lease_generation"]) + 1
    await session.execute(
        text(
            "UPDATE conversation.conversation_outbox "
            "SET status = 'processing', lease_owner = :owner, "
            "    lease_generation = :generation, lease_expires_at = :expires "
            "WHERE event_id = :event_id AND lease_generation = :current"
        ),
        {
            "owner": worker_id,
            "generation": generation,
            "expires": datetime.now(UTC) + timedelta(seconds=30),
            "event_id": row_dict["event_id"],
            "current": int(row_dict["lease_generation"]),
        },
    )
    row_dict["status"] = "processing"
    row_dict["lease_owner"] = worker_id
    row_dict["lease_generation"] = generation
    return row_dict
