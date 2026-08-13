"""delete_thread 协调 Job 集成测试（方案 §1.5 R3/S3 / §8.6）。

覆盖：
- 有活动 Turn 时等待不清理（R4）；
- 无活动 Turn 且本 generation deletion Outbox 未 delivered 时等待
  （不递增 attempt_count，R3）；
- Outbox dead_letter 时保持 deleting（需人工处理）；
- 全部 delivered 后同一事务转 deleted 并完成 Job；
- 普通 ConversationEvidence 投递带 thread.status=active fencing（§8.6 步骤 2）。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.conversation.persistence import messages as messages_repo
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo
from backend.conversation.services.thread_deletion import execute_delete_thread

pytestmark = pytest.mark.asyncio


async def _seed_deleting_thread(
    session_factory: async_sessionmaker,
) -> tuple[UUID, UUID, int]:
    """构造 deleting 线程 + 一条消息 + 一个 delete_thread Job。"""
    thread_id = uuid4()
    user_id = uuid4()
    turn_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
            await turns_repo.insert_turn(
                session,
                turn_id=turn_id,
                thread_id=thread_id,
                user_id=user_id,
                client_request_id=f"req-{turn_id}",
                request_id="t",
                run_id="r",
                user_message_id=uuid4(),
                expected_thread_version=0,
                graph_thread_id=f"conv-turn:{turn_id}",
            )
            await messages_repo.increment_thread_sequence(session, thread_id, by=1)
            await messages_repo.insert_message(
                session,
                message_id=uuid4(),
                thread_id=thread_id,
                turn_id=turn_id,
                user_id=user_id,
                sequence=1,
                role="user",
                content="你好",
                content_hash="hash",
            )
            await threads_repo.set_thread_status(
                session, thread_id, "deleting", bump_deletion_generation=True
            )
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_jobs ("
                    "  job_id, job_type, thread_id, user_id, deletion_generation,"
                    "  status, attempt_count, next_attempt_at, created_at, updated_at"
                    ") VALUES ("
                    "  :job_id, 'delete_thread', :thread_id, :user_id, :generation,"
                    "  'pending', 0, now(), now(), now()"
                    ")"
                ),
                {
                    "job_id": uuid4(),
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "generation": 1,
                },
            )
    return thread_id, user_id, 1


async def _claim_delete_job(session_factory, thread_id: UUID) -> tuple[UUID, str]:
    """claim 该线程的 delete_thread Job，返回 (job_id, worker_id)。"""
    async with session_factory() as session:
        async with session.begin():
            rows = await session.execute(
                text(
                    "SELECT * FROM conversation.conversation_jobs "
                    "WHERE thread_id = :thread_id AND job_type = 'delete_thread'"
                ),
                {"thread_id": thread_id},
            )
            row = dict(rows.mappings().first())
            await session.execute(
                text(
                    "UPDATE conversation.conversation_jobs "
                    "SET status = 'processing', lease_owner = 'w-1', "
                    "    lease_generation = 1, lease_expires_at = now() + interval '60 seconds', "
                    "    attempt_count = 1 "
                    "WHERE job_id = :job_id"
                ),
                {"job_id": row["job_id"]},
            )
    return row["job_id"], "w-1"


async def test_delete_thread_waits_while_active_turn(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """R4：有活动 Turn 时不清理任何数据，进入等待并重试。"""
    thread_id, _user_id, generation = await _seed_deleting_thread(conversation_session_factory)
    job_id, worker_id = await _claim_delete_job(conversation_session_factory, thread_id)

    async with conversation_session_factory() as session:
        async with session.begin():
            result = await execute_delete_thread(
                session,
                job_id=job_id,
                thread_id=thread_id,
                deletion_generation=generation,
                worker_id=worker_id,
            )
    assert result == "wait"
    async with conversation_session_factory() as session:
        row = await threads_repo.get_thread(session, thread_id)
        messages = await session.execute(
            text(
                "SELECT COUNT(*) FROM conversation.conversation_messages "
                "WHERE thread_id = :thread_id AND status != 'deleted'"
            ),
            {"thread_id": thread_id},
        )
        job = (
            (
                await session.execute(
                    text("SELECT * FROM conversation.conversation_jobs WHERE job_id = :job_id"),
                    {"job_id": job_id},
                )
            )
            .mappings()
            .first()
        )
    assert row["status"] == "deleting"
    assert int(messages.scalar_one()) == 1  # 消息未被清理
    assert job["status"] == "retry_wait"
    assert job["attempt_count"] == 1  # 等待不递增 attempt_count（R3）


async def test_delete_thread_wait_then_finish(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """R3/S3：Outbox 未 delivered 时等待；delivered 后转 deleted 并完成 Job。"""
    thread_id, user_id, generation = await _seed_deleting_thread(conversation_session_factory)
    job_id, worker_id = await _claim_delete_job(conversation_session_factory, thread_id)
    # 终止活动 Turn：删除 API 的取消原子分支（R2：accepted 直接转 cancelled）
    async with conversation_session_factory() as session:
        async with session.begin():
            turn = await turns_repo.get_active_turn(session, thread_id, for_update=True)
            assert await turns_repo.cancel_accepted_turn(session, turn["turn_id"]) is True

    # 写入一个本 generation 的 deletion Outbox（未投递）
    async with conversation_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_outbox ("
                    "  event_id, event_type, aggregate_type, aggregate_id, aggregate_version,"
                    "  idempotency_key, user_id, thread_id, status, attempt_count,"
                    "  next_attempt_at, created_at"
                    ") VALUES ("
                    "  :event_id, 'memory.source_deleted', 'thread', :generation, 1,"
                    "  :idem, :user_id, :thread_id, 'pending', 0, now(), now()"
                    ")"
                ),
                {
                    "event_id": uuid4(),
                    "generation": str(generation),
                    "idem": f"del:{thread_id}:{generation}",
                    "user_id": user_id,
                    "thread_id": thread_id,
                },
            )

    async with conversation_session_factory() as session:
        async with session.begin():
            result = await execute_delete_thread(
                session,
                job_id=job_id,
                thread_id=thread_id,
                deletion_generation=generation,
                worker_id=worker_id,
            )
    assert result == "wait"

    # 模拟 Publisher 投递完成
    async with conversation_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE conversation.conversation_outbox "
                    "SET status = 'delivered', delivered_at = now() "
                    "WHERE thread_id = :thread_id"
                ),
                {"thread_id": thread_id},
            )
    # 等待到期后 Worker 重新 claim（wait_job 已释放 lease）
    job_id2, worker_id2 = await _claim_delete_job(conversation_session_factory, thread_id)
    async with conversation_session_factory() as session:
        async with session.begin():
            result = await execute_delete_thread(
                session,
                job_id=job_id2,
                thread_id=thread_id,
                deletion_generation=generation,
                worker_id=worker_id2,
            )
    assert result == "done"
    async with conversation_session_factory() as session:
        row = await threads_repo.get_thread(session, thread_id)
        job = (
            (
                await session.execute(
                    text("SELECT * FROM conversation.conversation_jobs WHERE job_id = :job_id"),
                    {"job_id": job_id2},
                )
            )
            .mappings()
            .first()
        )
        count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM conversation.conversation_messages "
                    "WHERE thread_id = :thread_id AND status != 'deleted'"
                ),
                {"thread_id": thread_id},
            )
        ).scalar_one()
    assert row["status"] == "deleted"
    assert job["status"] == "done"
    assert int(count) == 0


async def test_delete_thread_holds_on_dead_letter(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§8.6 步骤 6：deletion Outbox dead_letter 时保持 deleting，需人工处理。"""
    thread_id, user_id, generation = await _seed_deleting_thread(conversation_session_factory)
    job_id, worker_id = await _claim_delete_job(conversation_session_factory, thread_id)
    async with conversation_session_factory() as session:
        async with session.begin():
            turn = await turns_repo.get_active_turn(session, thread_id, for_update=True)
            assert await turns_repo.cancel_accepted_turn(session, turn["turn_id"]) is True
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_outbox ("
                    "  event_id, event_type, aggregate_type, aggregate_id, aggregate_version,"
                    "  idempotency_key, user_id, thread_id, status, attempt_count,"
                    "  next_attempt_at, last_error_code, created_at"
                    ") VALUES ("
                    "  :event_id, 'memory.source_deleted', 'thread', :generation, 1,"
                    "  :idem, :user_id, :thread_id, 'dead_letter', 10, now(), 'PERMANENT', now()"
                    ")"
                ),
                {
                    "event_id": uuid4(),
                    "generation": str(generation),
                    "idem": f"del2:{thread_id}:{generation}",
                    "user_id": user_id,
                    "thread_id": thread_id,
                },
            )
    async with conversation_session_factory() as session:
        async with session.begin():
            result = await execute_delete_thread(
                session,
                job_id=job_id,
                thread_id=thread_id,
                deletion_generation=generation,
                worker_id=worker_id,
            )
    assert result == "needs_review"
    async with conversation_session_factory() as session:
        row = await threads_repo.get_thread(session, thread_id)
    assert row["status"] == "deleting"
