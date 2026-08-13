"""JobWorker 集成测试（方案 §7.7 / 评审测试缺口）。

覆盖：
- generate_title：模型失败取首条用户消息前 20 字符兜底（§7.6）；
- summarize_thread：摘要写入 conversation_summaries；
- 真实失败受 CONVERSATION_JOB_MAX_ATTEMPTS 约束转 dead_letter（评审 P2）；
- 崩溃遗留 processing Job 可回收（评审 C5）。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from backend.conversation.persistence import jobs as jobs_repo
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.services.token_counter import TokenCounter, WhitespaceTokenizer
from backend.conversation.worker.job_worker import JobWorker

pytestmark = pytest.mark.asyncio


class FakeOpenAI:
    """Fake OpenAI Gateway：可脚本化失败。"""

    def __init__(self) -> None:
        self.fail = False
        self.summary = "测试摘要"

    async def summarize_conversation(
        self, *, messages: list[dict], previous_summary: str | None
    ) -> str:
        if self.fail:
            raise RuntimeError("模型不可用")
        return self.summary


async def _seed_thread_with_message(
    session_factory: async_sessionmaker,
) -> tuple[UUID, UUID]:
    thread_id = uuid4()
    user_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_messages ("
                    "  message_id, thread_id, turn_id, user_id, sequence, role, content,"
                    "  status, content_hash, eligible_for_context, eligible_for_memory,"
                    "  occurred_at"
                    ") VALUES ("
                    "  :mid, :thread_id, :turn_id, :user_id, 1, 'user', :content,"
                    "  'completed', 'hash', true, true, now()"
                    ")"
                ),
                {
                    "mid": uuid4(),
                    "thread_id": thread_id,
                    "turn_id": uuid4(),
                    "user_id": user_id,
                    "content": "请讲解等差数列求和公式",
                },
            )
    return thread_id, user_id


def _job_worker(
    session_factory: async_sessionmaker,
    openai: FakeOpenAI,
    *,
    max_attempts: int = 10,
) -> JobWorker:
    return JobWorker(
        session_factory=session_factory,
        config=type("C", (), {"conversation_job_max_attempts": max_attempts})(),
        openai_gateway=openai,
        token_counter=TokenCounter(tokenizer=WhitespaceTokenizer()),
        worker_id="job-test",
    )


async def test_generate_title_with_model_fallback(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§7.6：模型失败取首条用户消息前 20 字符兜底。"""
    thread_id, user_id = await _seed_thread_with_message(conversation_session_factory)
    openai = FakeOpenAI()
    openai.fail = True  # 模型失败
    worker = _job_worker(conversation_session_factory, openai)
    job_id = uuid4()
    async with conversation_session_factory() as session:
        async with session.begin():
            await jobs_repo.insert_job(
                session,
                job_id=job_id,
                job_type="generate_title",
                thread_id=thread_id,
                user_id=user_id,
                target_sequence=0,
            )
            await session.execute(
                text(
                    "UPDATE conversation.conversation_jobs "
                    "SET status = 'processing', lease_owner = 'job-test', "
                    "    lease_generation = 1, attempt_count = 1 "
                    "WHERE job_id = :id"
                ),
                {"id": job_id},
            )
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM conversation.conversation_jobs WHERE job_id = :id"),
                        {"id": job_id},
                    )
                )
                .mappings()
                .first()
            )
    await worker._execute_job(dict(row))
    async with conversation_session_factory() as session:
        thread = await threads_repo.get_thread(session, thread_id)
        job = (
            (
                await session.execute(
                    text("SELECT * FROM conversation.conversation_jobs WHERE job_id = :id"),
                    {"id": job_id},
                )
            )
            .mappings()
            .first()
        )
    # 兜底标题：首条用户消息前 20 字符（模型失败时）
    assert thread["title"] == "请讲解等差数列求和公式"[:20]
    assert job["status"] == "done"


async def test_summarize_thread_writes_summary(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """§7.6：摘要写入 conversation_summaries。"""
    thread_id, user_id = await _seed_thread_with_message(conversation_session_factory)
    worker = _job_worker(conversation_session_factory, FakeOpenAI())
    job_id = uuid4()
    async with conversation_session_factory() as session:
        async with session.begin():
            await jobs_repo.insert_job(
                session,
                job_id=job_id,
                job_type="summarize_thread",
                thread_id=thread_id,
                user_id=user_id,
            )
            await session.execute(
                text(
                    "UPDATE conversation.conversation_jobs "
                    "SET status = 'processing', lease_owner = 'job-test', "
                    "    lease_generation = 1, attempt_count = 1 "
                    "WHERE job_id = :id"
                ),
                {"id": job_id},
            )
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM conversation.conversation_jobs WHERE job_id = :id"),
                        {"id": job_id},
                    )
                )
                .mappings()
                .first()
            )
    await worker._execute_job(dict(row))
    async with conversation_session_factory() as session:
        summary = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM conversation.conversation_summaries "
                        "WHERE thread_id = :thread_id"
                    ),
                    {"thread_id": thread_id},
                )
            )
            .mappings()
            .first()
        )
    assert summary is not None
    assert summary["content"] == "测试摘要"


async def test_job_failure_exhausts_attempts_to_dead_letter(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """评审 P2：真实失败达 max_attempts 转 dead_letter，不无限重试。"""
    thread_id, user_id = await _seed_thread_with_message(conversation_session_factory)
    openai = FakeOpenAI()
    openai.fail = True
    worker = _job_worker(conversation_session_factory, openai, max_attempts=1)
    job_id = uuid4()
    async with conversation_session_factory() as session:
        async with session.begin():
            await jobs_repo.insert_job(
                session,
                job_id=job_id,
                job_type="summarize_thread",
                thread_id=thread_id,
                user_id=user_id,
            )
            await session.execute(
                text(
                    "UPDATE conversation.conversation_jobs "
                    "SET status = 'processing', lease_owner = 'job-test', "
                    "    lease_generation = 1, attempt_count = 1 "
                    "WHERE job_id = :id"
                ),
                {"id": job_id},
            )
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM conversation.conversation_jobs WHERE job_id = :id"),
                        {"id": job_id},
                    )
                )
                .mappings()
                .first()
            )
    await worker._execute_job(dict(row))
    async with conversation_session_factory() as session:
        job = (
            (
                await session.execute(
                    text("SELECT * FROM conversation.conversation_jobs WHERE job_id = :id"),
                    {"id": job_id},
                )
            )
            .mappings()
            .first()
        )
    assert job["status"] == "dead_letter"


async def test_claim_reclaims_stale_processing_job(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """评审 C5：崩溃遗留 processing（lease 过期）可被 claim 回收。"""
    thread_id, user_id = await _seed_thread_with_message(conversation_session_factory)
    job_id = uuid4()
    async with conversation_session_factory() as session:
        async with session.begin():
            await jobs_repo.insert_job(
                session,
                job_id=job_id,
                job_type="generate_title",
                thread_id=thread_id,
                user_id=user_id,
                target_sequence=0,
            )
            await session.execute(
                text(
                    "UPDATE conversation.conversation_jobs "
                    "SET status = 'processing', lease_owner = 'dead-worker', "
                    "    lease_generation = 3, lease_expires_at = now() - interval '10 seconds' "
                    "WHERE job_id = :id"
                ),
                {"id": job_id},
            )
    async with conversation_session_factory() as session:
        async with session.begin():
            rows = await jobs_repo.claim_jobs(
                session, worker_id="new-worker", lease_seconds=60, limit=5
            )
    assert any(row["job_id"] == job_id for row in rows)
    claimed = next(row for row in rows if row["job_id"] == job_id)
    assert claimed["lease_owner"] == "new-worker"
    assert claimed["lease_generation"] == 4


async def test_summarize_uses_anchor_sequence(
    conversation_session_factory: async_sessionmaker,
) -> None:
    """第四轮 Nit：摘要以 target_sequence 为锚点——锚点之后无新消息时不写摘要。"""
    thread_id, user_id = await _seed_thread_with_message(conversation_session_factory)
    worker = _job_worker(conversation_session_factory, FakeOpenAI())
    job_id = uuid4()
    async with conversation_session_factory() as session:
        async with session.begin():
            # 锚点 = 1（最新摘要序号之后的游标）；线程只有 sequence=1 一条消息
            await jobs_repo.insert_job(
                session,
                job_id=job_id,
                job_type="summarize_thread",
                thread_id=thread_id,
                user_id=user_id,
                target_sequence=1,
            )
            await session.execute(
                text(
                    "UPDATE conversation.conversation_jobs "
                    "SET status = 'processing', lease_owner = 'job-test', "
                    "    lease_generation = 1, attempt_count = 1 "
                    "WHERE job_id = :id"
                ),
                {"id": job_id},
            )
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM conversation.conversation_jobs WHERE job_id = :id"),
                        {"id": job_id},
                    )
                )
                .mappings()
                .first()
            )
    await worker._execute_job(dict(row))
    async with conversation_session_factory() as session:
        summary = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM conversation.conversation_summaries "
                        "WHERE thread_id = :thread_id"
                    ),
                    {"thread_id": thread_id},
                )
            )
            .mappings()
            .first()
        )
        job = (
            (
                await session.execute(
                    text("SELECT * FROM conversation.conversation_jobs WHERE job_id = :id"),
                    {"id": job_id},
                )
            )
            .mappings()
            .first()
        )
    # 锚点之后无新消息：不写摘要、Job 直接完成（不产生重叠摘要）
    assert summary is None
    assert job["status"] == "done"
