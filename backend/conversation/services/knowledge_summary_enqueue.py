"""知识总结自动 enqueue 修复服务（知识总结方案 §14.1）。

负责在 Worker 扫描周期内修复 `enqueue_failed` 的 Turn：重新校验运行条件、
Thread/Turn 状态、tombstone 和来源 checkpoint，然后幂等地创建自动 Generation Job。
失败时按无上限确定性退避更新 Turn 状态，不进入 dead_letter。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.conversation.persistence import (
    knowledge_summaries as summaries_repo,
)
from backend.conversation.persistence import (
    knowledge_summary_generations as generations_repo,
)
from backend.settings import Settings


def _enqueue_backoff_seconds(attempts: int) -> int:
    """无上限确定性退避：30s → 60s → 120s → 240s → 300s → 300s…"""
    return int(min(30 * (2 ** (attempts - 1)), 300))


class KnowledgeSummaryEnqueueRepairService:
    """修复 finalize 中未能成功入队的知识总结自动 Job。"""

    def __init__(
        self,
        *,
        settings: Settings,
    ) -> None:
        self._settings = settings

    async def repair_turn(
        self,
        session: AsyncSession,
        *,
        turn_row: dict[str, Any],
    ) -> None:
        """对单个 enqueue_failed Turn 尝试修复；调用方已持有行锁。"""
        turn_id = UUID(str(turn_row["turn_id"]))
        thread_id = UUID(str(turn_row["thread_id"]))
        user_id = UUID(str(turn_row["user_id"]))
        source_checkpoint_id = str(turn_row.get("source_checkpoint_id") or "")
        attempts = int(turn_row.get("knowledge_summary_enqueue_attempts", 0))

        flags = self._settings.knowledge_summary_flags
        if not (flags["enabled"] and flags["generation"] and flags["auto_generate"]):
            await summaries_repo.update_knowledge_summary_enqueue_status(
                session,
                turn_id=turn_id,
                status="not_requested",
                attempts_delta=0,
                next_attempt_at=None,
            )
            return

        runtime_control = await summaries_repo.get_runtime_control(session)
        if runtime_control is not None and runtime_control["auto_generation_suspended"]:
            # 暂停期间不增加 attempts、不移动 next_attempt_at；恢复后立即处理。
            return

        # 终止条件：Thread 删除中/已删除、Turn 不再是 completed、checkpoint 永久缺失。
        thread_row = await session.execute(
            text(
                "SELECT status FROM conversation.conversation_threads WHERE thread_id = :thread_id"
            ),
            {"thread_id": thread_id},
        )
        first_row = thread_row.mappings().first()
        thread_status = first_row["status"] if first_row is not None else None
        if thread_status not in ("active", "archived"):
            await summaries_repo.update_knowledge_summary_enqueue_status(
                session,
                turn_id=turn_id,
                status="not_requested",
                attempts_delta=0,
                next_attempt_at=None,
            )
            return

        if str(turn_row.get("status")) != "completed" or not source_checkpoint_id:
            await summaries_repo.update_knowledge_summary_enqueue_status(
                session,
                turn_id=turn_id,
                status="not_requested",
                attempts_delta=0,
                next_attempt_at=None,
            )
            return

        # Tombstone 抑制：旧 Turn 命中墓碑时不再重试。
        tombstone_turns = await summaries_repo.list_tombstone_turns(
            session, user_id=user_id, turn_id=turn_id
        )
        if tombstone_turns:
            await summaries_repo.update_knowledge_summary_enqueue_status(
                session,
                turn_id=turn_id,
                status="not_requested",
                attempts_delta=0,
                next_attempt_at=None,
            )
            return

        # 读取主来源 user message 的 occurred_at。
        result = await session.execute(
            text(
                "SELECT occurred_at FROM conversation.conversation_messages "
                "WHERE thread_id = :thread_id AND turn_id = :turn_id AND role = 'user' "
                "  AND status = 'completed' "
                "ORDER BY sequence LIMIT 1"
            ),
            {"thread_id": thread_id, "turn_id": turn_id},
        )
        msg_row = result.mappings().first()
        primary_occurred_at = msg_row["occurred_at"] if msg_row is not None else datetime.now(UTC)

        next_attempts = attempts + 1
        now = datetime.now(UTC)
        try:
            generation_id = uuid4()
            idempotency_key = f"knowledge-summary:auto:{turn_id}:{source_checkpoint_id}"
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
                await summaries_repo.update_knowledge_summary_enqueue_status(
                    session,
                    turn_id=turn_id,
                    status="enqueued",
                    attempts_delta=1,
                    next_attempt_at=None,
                )
            else:
                # 幂等命中：已有自动 Job。
                await summaries_repo.update_knowledge_summary_enqueue_status(
                    session,
                    turn_id=turn_id,
                    status="enqueued",
                    attempts_delta=0,
                    next_attempt_at=None,
                )
        except Exception:
            next_attempt = now + timedelta(seconds=_enqueue_backoff_seconds(next_attempts))
            await summaries_repo.update_knowledge_summary_enqueue_status(
                session,
                turn_id=turn_id,
                status="enqueue_failed",
                attempts_delta=1,
                next_attempt_at=next_attempt,
            )
