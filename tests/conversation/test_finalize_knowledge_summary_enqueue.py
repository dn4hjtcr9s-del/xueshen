"""finalize 节点自动入队知识总结 Job 的集成测试（方案 §14.1）。

验证三级开关开启时，answer.completed 主事务通过 savepoint 创建幂等 auto Job；
开关关闭时保持 not_requested；局部失败不回滚回答主事务。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.graph.nodes.finalize import persist_turn
from backend.conversation.graph.state import ConversationRuntimeContext
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo
from backend.conversation.persistence.repository import ConversationRepository
from backend.settings import Settings


class FakeClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeIdGenerator:
    def __init__(self) -> None:
        self._counter = 0

    def __call__(self) -> str:
        self._counter += 1
        return f"id-{self._counter}"


class FakeTurnEventWriter:
    async def append(self, session: AsyncSession, *, write: Any) -> None:
        return


def _build_runtime(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    worker_id: str,
) -> ConversationRuntimeContext:
    runtime = ConversationRuntimeContext(
        openai_gateway=None,
        memory_gateway=None,
        embedding_gateway=None,
        retriever_gateway=None,
        conversation_repository=ConversationRepository(session_factory=session_factory),
        turn_event_writer=FakeTurnEventWriter(),
        clock=FakeClock(),
        id_generator=FakeIdGenerator(),
        logger=logging.getLogger("test"),
        flags=settings.conversation_flags,
        worker_id=worker_id,
    )
    runtime.settings = settings
    runtime.token_counter = None
    return runtime


async def _insert_running_turn(
    session: AsyncSession,
    *,
    user_id: UUID,
    worker_id: str,
) -> tuple[UUID, UUID, UUID]:
    """插入 active thread、running turn，供 finalize 使用。

    finalize 自身会写入 assistant message 并生成 source_checkpoint_id；
    本 helper 不预写消息，避免 sequence 冲突。
    """
    thread_id = uuid4()
    turn_id = uuid4()
    user_message_id = uuid4()
    await threads_repo.insert_thread(session, thread_id, user_id)
    await turns_repo.insert_turn(
        session,
        turn_id=turn_id,
        thread_id=thread_id,
        user_id=user_id,
        client_request_id=f"finalize-{turn_id}",
        request_id=f"request-{turn_id}",
        run_id=f"run-{turn_id}",
        user_message_id=user_message_id,
        expected_thread_version=0,
        graph_thread_id=str(thread_id),
        next_attempt_at=datetime.now(UTC),
    )
    await session.execute(
        text(
            "UPDATE conversation.conversation_turns "
            "SET status = 'running', lease_owner = :worker_id, lease_generation = 1 "
            "WHERE turn_id = :turn_id"
        ),
        {"turn_id": turn_id, "worker_id": worker_id},
    )
    return thread_id, turn_id, user_message_id


@pytest.mark.asyncio
async def test_finalize_creates_auto_knowledge_summary_job_when_enabled(
    conversation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """三级开关开启时 finalize 为 completed Turn 创建 auto Generation Job。"""
    user_id = uuid4()
    worker_id = "test-worker"
    settings = Settings(
        app_env="test",
        _env_file=None,
        conversation_knowledge_summary_enabled=True,
        conversation_knowledge_summary_generation_enabled=True,
        conversation_knowledge_summary_auto_generate_enabled=True,
        openai_knowledge_summary_model="contract-test-model",
        conversation_knowledge_summary_structured_output_models="contract-test-model",
        conversation_knowledge_summary_daily_token_budget=10000,
    )
    runtime = _build_runtime(conversation_session_factory, settings, worker_id)

    async with conversation_session_factory() as session:
        async with session.begin():
            thread_id, turn_id, user_message_id = await _insert_running_turn(
                session, user_id=user_id, worker_id=worker_id
            )

    state = {
        "turn_id": turn_id,
        "thread_id": thread_id,
        "user_id": user_id,
        "user_message_id": user_message_id,
        "request_id": "req",
        "run_id": "run",
        "answer_payload": {"answer": "椭圆离心率 e=c/a。"},
    }
    result = await persist_turn(state, runtime=runtime)

    assert result["assistant_message_id"] is not None
    assert result["source_checkpoint_id"]

    async with conversation_session_factory() as session:
        async with session.begin():
            turn = (
                (
                    await session.execute(
                        text(
                            "SELECT status, knowledge_summary_enqueue_status "
                            "FROM conversation.conversation_turns WHERE turn_id = :turn_id"
                        ),
                        {"turn_id": turn_id},
                    )
                )
                .mappings()
                .one()
            )
            jobs = (
                (
                    await session.execute(
                        text(
                            "SELECT trigger, status "
                            "FROM conversation.knowledge_summary_generation_jobs "
                            "WHERE turn_id = :turn_id"
                        ),
                        {"turn_id": turn_id},
                    )
                )
                .mappings()
                .all()
            )

    assert turn["status"] == "completed"
    assert turn["knowledge_summary_enqueue_status"] == "enqueued"
    assert len(jobs) == 1
    assert jobs[0]["trigger"] == "auto"
    assert jobs[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_finalize_skips_auto_job_when_auto_disabled(
    conversation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """自动生成开关关闭时 finalize 不创建 Job，但回答仍成功提交。"""
    user_id = uuid4()
    worker_id = "test-worker"
    settings = Settings(
        app_env="test",
        _env_file=None,
        conversation_knowledge_summary_enabled=True,
        conversation_knowledge_summary_generation_enabled=True,
        conversation_knowledge_summary_auto_generate_enabled=False,
        openai_knowledge_summary_model="contract-test-model",
        conversation_knowledge_summary_structured_output_models="contract-test-model",
    )
    runtime = _build_runtime(conversation_session_factory, settings, worker_id)

    async with conversation_session_factory() as session:
        async with session.begin():
            thread_id, turn_id, user_message_id = await _insert_running_turn(
                session, user_id=user_id, worker_id=worker_id
            )

    state = {
        "turn_id": turn_id,
        "thread_id": thread_id,
        "user_id": user_id,
        "user_message_id": user_message_id,
        "request_id": "req",
        "run_id": "run",
        "answer_payload": {"answer": "椭圆离心率 e=c/a。"},
    }
    await persist_turn(state, runtime=runtime)

    async with conversation_session_factory() as session:
        async with session.begin():
            turn = (
                (
                    await session.execute(
                        text(
                            "SELECT status, knowledge_summary_enqueue_status "
                            "FROM conversation.conversation_turns WHERE turn_id = :turn_id"
                        ),
                        {"turn_id": turn_id},
                    )
                )
                .mappings()
                .one()
            )
            job_count = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM conversation.knowledge_summary_generation_jobs "
                        "WHERE turn_id = :turn_id"
                    ),
                    {"turn_id": turn_id},
                )
            ).scalar_one()

    assert turn["status"] == "completed"
    assert turn["knowledge_summary_enqueue_status"] == "not_requested"
    assert int(job_count) == 0
