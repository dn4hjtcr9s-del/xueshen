"""delete_thread 协调 Job 执行器（方案 §1.5 R3/S3 / §8.6）。

delete_thread 是 deleting → deleted 的唯一协调器：
- 清理前必须确认该线程无 accepted/running/cancelling Turn（R4）；有则等待重试；
- 本地清理包含知识总结 Job 取消、来源失效和计数重算；不删除总结正文或 Revision；
- 本地清理完成后检查本 deletion_generation 全部 deletion Outbox：
  仍有 pending/processing/retry_wait 时进入 waiting + next_attempt_at
  （不递增 attempt_count，R3）；
- 任意 Outbox dead_letter 或本地清理失败时保持 deleting、Job 标记需人工处理并告警；
- 只有本地清理完成且本 generation 全部 deletion Outbox delivered 时，
  同一事务把 Thread 置为 deleted 并完成自身。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from backend.conversation.persistence import events as events_repo
from backend.conversation.persistence import jobs as jobs_repo
from backend.conversation.persistence import knowledge_summaries as knowledge_summaries_repo
from backend.conversation.persistence import messages as messages_repo
from backend.conversation.persistence import outbox as outbox_repo
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo

DELETION_WAIT_SECONDS = 15
REVIEW_WAIT_SECONDS = 60


async def execute_delete_thread(
    session: AsyncSession,
    *,
    job_id: UUID,
    thread_id: UUID,
    deletion_generation: int,
    worker_id: str,
) -> str:
    """执行 delete_thread Job 一个周期；返回结果分支（done / wait / needs_review）。"""
    # 1. 确认无活动 Turn（R4）；仍有活动 Turn 时不清理任何数据，只等待重试
    active = await turns_repo.get_active_turn(session, thread_id, for_update=True)
    if active is not None:
        await jobs_repo.wait_job(
            session,
            job_id,
            worker_id=worker_id,
            wait_seconds=DELETION_WAIT_SECONDS,
            error_code="ACTIVE_TURN_STILL_RUNNING",
        )
        return "wait"

    # 2. 本地清理：知识总结与会话消息独立维护，终态 Turn 才能删。
    # processing 的知识总结 Job 由 Thread deleting 状态与 Worker fencing 在提交前自行取消。
    await knowledge_summaries_repo.cancel_generation_jobs_for_thread(session, thread_id=thread_id)
    affected_summary_ids = await knowledge_summaries_repo.mark_sources_unavailable_for_thread(
        session, thread_id=thread_id
    )
    await knowledge_summaries_repo.lock_and_recalculate_source_counts(
        session, summary_ids=affected_summary_ids
    )
    await messages_repo.mark_messages_deleted_for_thread(session, thread_id)
    await events_repo.delete_events_for_thread(session, thread_id)
    await _delete_summaries_for_thread(session, thread_id)

    # 3. 检查本 generation 全部 deletion Outbox（R3/S3）
    deletions = await outbox_repo.list_outbox_by_thread(
        session, thread_id, deletion_generation=deletion_generation
    )
    if not deletions:
        # 本 generation 无删除 Outbox（例如线程无已投递来源）：直接完成
        await _finish_deleted(session, thread_id=thread_id, job_id=job_id, worker_id=worker_id)
        return "done"
    pending = [row for row in deletions if row["status"] in ("pending", "processing", "retry_wait")]
    dead = [row for row in deletions if row["status"] == "dead_letter"]
    if pending:
        # 等待：不递增 attempt_count（R3）
        await jobs_repo.wait_job(
            session,
            job_id,
            worker_id=worker_id,
            wait_seconds=DELETION_WAIT_SECONDS,
            error_code="DELETION_OUTBOX_NOT_DELIVERED",
        )
        return "wait"
    if dead:
        # dead letter：保持 deleting，Job 标记需人工处理并告警
        await jobs_repo.wait_job(
            session,
            job_id,
            worker_id=worker_id,
            wait_seconds=REVIEW_WAIT_SECONDS,
            error_code="DELETION_OUTBOX_DEAD_LETTER",
        )
        logging.getLogger("conversation.worker").error(
            "delete_thread 卡在 deleting：deletion Outbox dead_letter，thread_id=%s generation=%d",
            thread_id,
            deletion_generation,
        )
        return "needs_review"

    # 4. 全部 delivered：同一事务完成
    await _finish_deleted(session, thread_id=thread_id, job_id=job_id, worker_id=worker_id)
    return "done"


async def _finish_deleted(
    session: AsyncSession,
    *,
    thread_id: UUID,
    job_id: UUID,
    worker_id: str,
) -> None:
    """本地清理完成且 deletion Outbox 全部 delivered → deleted + Job done（R3）。"""
    await threads_repo.set_thread_status(
        session, thread_id, "deleted", deleted_at=datetime.now(UTC)
    )
    await jobs_repo.mark_job_done(session, job_id, worker_id=worker_id)


async def _delete_summaries_for_thread(session: AsyncSession, thread_id: UUID) -> None:
    from sqlalchemy import text

    await session.execute(
        text("DELETE FROM conversation.conversation_summaries WHERE thread_id = :thread_id"),
        {"thread_id": thread_id},
    )
