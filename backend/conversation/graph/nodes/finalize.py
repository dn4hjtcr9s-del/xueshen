"""persist_turn（finalize）节点（方案 §5.2 / §16.3 / §1.5 R4-R5）。

同一事务原子完成：
1. 助手消息保存（只有完整生成并通过验证才 completed，§7.3）；
2. Thread version +1（eligible_for_context=true 的助手消息保存后，§1.5 R5）；
3. Memory Outbox 写入（普通对话 trigger=turn_boundary；MEMORY_SUBMIT_ENABLED=false
   时跳过，§附录 A.10）；
4. answer.completed 事件（§17.4.1，含 thread_version/answer/citations/followups）；
5. Turn 置 completed（携带 thread.status=active fencing，R4：发现 deleting 只能
   取消或清理，不能重新写完成消息或 Evidence）。

source_checkpoint_id 由 canonical manifest（build_source_manifest）生成（§7.2 / D9）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.conversation.contracts.domain import build_source_manifest
from backend.conversation.contracts.events import TurnEventWrite
from backend.conversation.graph.state import ConversationRuntimeContext
from backend.conversation.persistence import messages as messages_repo
from backend.conversation.persistence import outbox as outbox_repo
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo


async def persist_turn(
    state: dict[str, Any],
    *,
    runtime: ConversationRuntimeContext,
) -> dict[str, Any]:
    """finalize：助手消息 + Outbox + completed 事件原子事务（§5.2/§16.3）。"""
    repo = runtime.conversation_repository
    turn_id = state["turn_id"]
    thread_id = state["thread_id"]
    user_id = state["user_id"]
    payload = state.get("answer_payload") or {}
    answer = str(payload.get("answer") or "")
    citations = payload.get("citations") or []
    followups = (payload.get("followups") or [])[:3]
    degraded_flags = state.get("degraded_flags") or []
    request_id = str(state.get("request_id") or "")
    run_id = str(state.get("run_id") or "")
    flags = runtime.flags

    if repo is None or repo.session_factory is None:
        # 单测无 DB：返回合成结果（不写库）
        return {
            "assistant_message_id": str(uuid4()),
            "outbox_event_id": str(uuid4()) if flags.get("memory_submit", True) else None,
            "source_checkpoint_id": "conv-src-v1:test",
        }

    async with repo.session_factory() as session:
        async with session.begin():
            # C4（评审）：finalize 前先验证 lease fencing —— 锁定 Turn 行并确认
            # 仍是当前 worker 持有（status=running 且 lease_owner=worker_id）。
            # 失租 Worker 在此即中止，不写任何消息/Outbox/事件副作用。
            turn_row = await turns_repo.get_turn(session, turn_id, for_update=True)
            if (
                turn_row is None
                or turn_row["status"] != "running"
                or turn_row["lease_owner"] != runtime.worker_id
            ):
                runtime.logger.warning(
                    "finalize fencing 失败（失租或状态异常），中止副作用: turn=%s worker=%s",
                    turn_id,
                    runtime.worker_id,
                )
                return {"assistant_message_id": None, "outbox_event_id": None}

            # R4 fencing：Thread 必须 active 才能正常 finalize
            thread = await threads_repo.get_thread(session, thread_id, for_update=True)
            if thread is None or thread["status"] != "active":
                # deleting/deleted：只能取消终态或清理，不写完成消息/Evidence（R4）
                await turns_repo.write_cancelled_running(
                    session, turn_id, worker_id=runtime.worker_id
                )
                return {"assistant_message_id": None, "outbox_event_id": None}

            # 1. 助手消息
            assistant_message_id = uuid4()
            sequence = await messages_repo.increment_thread_sequence(session, thread_id, by=1)
            await messages_repo.insert_message(
                session,
                message_id=assistant_message_id,
                thread_id=thread_id,
                turn_id=turn_id,
                user_id=user_id,
                sequence=sequence,
                role="assistant",
                content=answer,
                content_hash=sha256(answer.encode()).hexdigest(),
                status="completed",
                eligible_for_context=True,
                eligible_for_memory=True,
                completed_at=datetime.now(UTC),
            )
            # 2. Thread version +1（助手消息保存后，§1.5 R5）
            new_version = await threads_repo.bump_thread_version(session, thread_id)

            # 3. source_checkpoint_id（canonical manifest，§7.2 / D9）
            message_rows = await repo.list_messages_for_manifest(session, thread_id, turn_id)
            source_checkpoint_id = build_source_manifest(thread_id, turn_id, message_rows)

            # 4. Memory Outbox（MEMORY_SUBMIT_ENABLED 门控，附录 A.10）
            outbox_event_id: str | None = None
            if flags.get("memory_submit", True):
                memory_trigger = str(state.get("memory_trigger") or "turn_boundary")
                idempotency_key = f"conversation-evidence:{turn_id}:{source_checkpoint_id}"
                outbox_event_id = str(uuid4())
                await outbox_repo.insert_outbox(
                    session,
                    event_id=UUID(outbox_event_id),
                    event_type="conversation_evidence",
                    aggregate_type="conversation_turn",
                    aggregate_id=str(turn_id),
                    aggregate_version=1,
                    idempotency_key=idempotency_key,
                    user_id=user_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    message_ids=[state["user_message_id"], assistant_message_id],
                    source_checkpoint_id=source_checkpoint_id,
                    trigger=memory_trigger,
                    topic_hints=list(state.get("rewrite_plan", {}).get("topic_hints") or []),
                    graph_node_hints=_validated_graph_hints(state),
                )

            # 5. answer.completed 事件（与消息/Outbox 同事务，§17.5 #7）
            await runtime.turn_event_writer.append(
                session,
                write=TurnEventWrite(
                    turn_id=turn_id,
                    event_type="answer.completed",
                    request_id=request_id,
                    run_id=run_id,
                    payload={
                        "assistant_message_id": str(assistant_message_id),
                        "thread_version": new_version,
                        "answer": answer,
                        "citations": citations,
                        "followups": followups,
                        "degraded_flags": degraded_flags,
                    },
                ),
            )
            # 6. Turn 终态（携带 lease fencing：仅当前 worker 可写；检查行数，
            #    0 行说明失租/状态已变，本事务内不提交任何副作用——见下方 result）
            result = await session.execute(
                text(
                    "UPDATE conversation.conversation_turns "
                    "SET status = 'completed', assistant_message_id = :assistant_id, "
                    "    source_checkpoint_id = :checkpoint_id, "
                    "    memory_submission_status = :mem_status, "
                    "    updated_at = :now "
                    "WHERE turn_id = :turn_id AND status = 'running' "
                    "  AND lease_owner = :owner AND lease_generation = :generation"
                ),
                {
                    "assistant_id": assistant_message_id,
                    "checkpoint_id": source_checkpoint_id,
                    "mem_status": ("pending" if outbox_event_id is not None else "not_required"),
                    "now": datetime.now(UTC),
                    "turn_id": turn_id,
                    "owner": runtime.worker_id,
                    "generation": int(turn_row["lease_generation"]),
                },
            )
            if result.rowcount != 1:
                # 终态写回被 fencing 拒绝（评审 C4）：旧执行者不得提交副作用。
                # 抛出内部信号使整个事务回滚，消息/Outbox/事件全部不落库。
                raise LeaseFencedError(f"finalize 终态写回被 fencing 拒绝: turn={turn_id}")
            # 7. 标题/摘要 Job 入队（评审 C8 / §7.6）：
            #    - generate_title：首 Turn 完成后（线程无标题）；
            #    - summarize_thread：未被现有摘要覆盖的消息累计 ≥ 8000 tokens。
            await _enqueue_title_and_summary_jobs(
                session,
                thread_id=thread_id,
                user_id=user_id,
                token_counter=runtime.token_counter,
            )
            # 8. 知识总结自动生成 Job 入队（Phase 4 / §14.1）。
            #    使用 savepoint 隔离，局部失败不回滚聊天主事务。
            await _enqueue_knowledge_summary_auto_job(
                session,
                runtime=runtime,
                thread_id=thread_id,
                turn_id=turn_id,
                user_id=user_id,
                source_checkpoint_id=source_checkpoint_id,
            )
    return {
        "assistant_message_id": str(assistant_message_id),
        "outbox_event_id": outbox_event_id,
        "source_checkpoint_id": source_checkpoint_id,
    }


class LeaseFencedError(Exception):
    """finalize 终态写回被 fencing 拒绝（评审 C4）：触发事务回滚，无副作用提交。"""


async def _enqueue_title_and_summary_jobs(
    session: AsyncSession,
    *,
    thread_id: UUID,
    user_id: UUID,
    token_counter: Any,
) -> None:
    """C8（评审 / §7.6）：首 Turn 完成后入队标题 Job；未覆盖消息达 8000 tokens 入队摘要 Job。

    - generate_title：线程标题为空时触发（首个 Turn 完成后）；
    - summarize_thread：未被现有摘要覆盖的消息累计 token ≥ 8000 时触发；
      摘要由 conversation-worker 的 JobWorker 可靠消费（§7.7），
      失败不阻塞主对话（§7.6）。
    """
    from backend.conversation.persistence import jobs as jobs_repo

    # 标题：线程无标题 → generate_title（target_sequence=0 表示全量）
    thread = await threads_repo.get_thread(session, thread_id)
    if thread is not None and not (thread.get("title") or ""):
        await jobs_repo.insert_job(
            session,
            job_id=uuid4(),
            job_type="generate_title",
            thread_id=thread_id,
            user_id=user_id,
            target_sequence=0,
        )

    # 摘要：累计未覆盖消息 tokens
    if token_counter is not None:
        uncovered_rows = (
            (
                await session.execute(
                    text(
                        "SELECT c.content FROM conversation.conversation_messages c "
                        "WHERE c.thread_id = :thread_id AND c.status = 'completed' "
                        "AND c.sequence > COALESCE(("
                        "  SELECT MAX(s.sequence) FROM conversation.conversation_summaries s "
                        "  WHERE s.thread_id = c.thread_id"
                        "), 0)"
                    ),
                    {"thread_id": thread_id},
                )
            )
            .mappings()
            .all()
        )
        total_tokens = sum(token_counter.count(str(row["content"])) for row in uncovered_rows)
        if total_tokens >= 8000:
            # P2（第三轮评审）：target_sequence 用"最新摘要序号"作为幂等锚点，
            # 避免 NULL 被普通唯一约束忽略导致重复入队；摘要消费后游标前移，
            # 新消息累计达标时可用新游标再次触发。
            latest = (
                await session.execute(
                    text(
                        "SELECT MAX(sequence) FROM conversation.conversation_summaries "
                        "WHERE thread_id = :thread_id"
                    ),
                    {"thread_id": thread_id},
                )
            ).scalar_one_or_none()
            await jobs_repo.insert_job(
                session,
                job_id=uuid4(),
                job_type="summarize_thread",
                thread_id=thread_id,
                user_id=user_id,
                target_sequence=int(latest or 0),
            )


async def _enqueue_knowledge_summary_auto_job(
    session: AsyncSession,
    *,
    runtime: ConversationRuntimeContext,
    thread_id: UUID,
    turn_id: UUID,
    user_id: UUID,
    source_checkpoint_id: str,
) -> None:
    """在 finalize 主事务内尝试为 completed Turn 创建知识总结自动 Job（§14.1）。

    - 三级开关或 runtime control 暂停时直接保持 not_requested；
    - 使用 savepoint 隔离 Job 插入，约束/序列化错误只回滚 savepoint；
    - 任何失败只将 Turn 标记为 enqueue_failed 并设置退避，不阻断回答。
    """
    settings = runtime.settings
    if settings is None:
        return
    flags = settings.knowledge_summary_flags
    if not (flags["enabled"] and flags["generation"] and flags["auto_generate"]):
        return

    from backend.conversation.persistence import (
        knowledge_summaries as summaries_repo,
    )

    runtime_control = await summaries_repo.get_runtime_control(session)
    if runtime_control is not None and runtime_control["auto_generation_suspended"]:
        return

    # 读取主来源 user message 的 occurred_at 作为冻结 Turn 时间。
    result = await session.execute(
        text(
            "SELECT occurred_at FROM conversation.conversation_messages "
            "WHERE thread_id = :thread_id AND turn_id = :turn_id AND role = 'user' "
            "  AND status = 'completed' "
            "ORDER BY sequence LIMIT 1"
        ),
        {"thread_id": thread_id, "turn_id": turn_id},
    )
    row = result.mappings().first()
    primary_occurred_at = row["occurred_at"] if row is not None else datetime.now(UTC)

    from backend.conversation.persistence import (
        knowledge_summary_generations as generations_repo,
    )

    generation_id = uuid4()
    idempotency_key = f"knowledge-summary:auto:{turn_id}:{source_checkpoint_id}"
    now = datetime.now(UTC)
    try:
        async with session.begin_nested():
            inserted = await generations_repo.insert_generation_job(
                session,
                generation_id=generation_id,
                idempotency_key=idempotency_key,
                client_request_id=None,
                user_id=user_id,
                thread_id=thread_id,
                turn_id=turn_id,
                source_checkpoint_id=source_checkpoint_id,
                trigger="auto",
                primary_turn_occurred_at=primary_occurred_at,
            )
            if inserted:
                await session.execute(
                    text(
                        "UPDATE conversation.conversation_turns "
                        "SET knowledge_summary_enqueue_status = 'enqueued', "
                        "    knowledge_summary_enqueue_attempts = 1, "
                        "    knowledge_summary_enqueue_next_attempt_at = NULL, "
                        "    updated_at = :now "
                        "WHERE turn_id = :turn_id"
                    ),
                    {"turn_id": turn_id, "now": now},
                )
            else:
                # 幂等命中：已有自动 Job，标记为 enqueued。
                await session.execute(
                    text(
                        "UPDATE conversation.conversation_turns "
                        "SET knowledge_summary_enqueue_status = 'enqueued', "
                        "    knowledge_summary_enqueue_attempts = "
                        "        GREATEST(knowledge_summary_enqueue_attempts, 1), "
                        "    updated_at = :now "
                        "WHERE turn_id = :turn_id"
                    ),
                    {"turn_id": turn_id, "now": now},
                )
    except Exception:
        # savepoint 已回滚；记录 enqueue_failed，30s 后修复扫描重试。
        next_attempt = now + timedelta(seconds=30)
        await session.execute(
            text(
                "UPDATE conversation.conversation_turns "
                "SET knowledge_summary_enqueue_status = 'enqueue_failed', "
                "    knowledge_summary_enqueue_attempts = "
                "        knowledge_summary_enqueue_attempts + 1, "
                "    knowledge_summary_enqueue_next_attempt_at = :next_attempt, "
                "    updated_at = :now "
                "WHERE turn_id = :turn_id"
            ),
            {"turn_id": turn_id, "next_attempt": next_attempt, "now": now},
        )


def _validated_graph_hints(state: dict[str, Any]) -> list[str]:
    """graph_node_hints allow-list（§16.3 / Q10）。

    只保留本轮 LearningContext.graph_states[].node_id ∪ recommendations[].node_id；
    其余丢弃并计 rejected-hint 指标（指标在节点层统计）。
    """
    memory = (state.get("snapshot") or {}).get("memory") or {}
    allowed: set[str] = set()
    for node in memory.get("graph_states") or []:
        allowed.add(str(node.get("node_id", "")))
    for rec in memory.get("recommendations") or []:
        allowed.add(str(rec.get("node_id", "")))
    hints = list((state.get("rewrite_plan") or {}).get("graph_node_hints") or [])
    kept = [h for h in hints if h in allowed and h]
    return kept
