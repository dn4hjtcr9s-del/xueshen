"""知识总结 Phase 3 Worker 集成测试（知识总结方案 §22.2–§22.3）。

使用 Fake Gateway 和 conversation_test 数据库验证冻结输入、来源写入、Revision、
Generation 终态和 Worker 不重复写入的基本全链路。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.contracts.knowledge_summary import (
    AppendItemMutation,
    CandidateItem,
    CreateSummaryPlan,
    KnowledgeCandidate,
    KnowledgeExtractionResult,
    KnowledgeMergePlanResult,
    MergeSummaryPlan,
)
from backend.conversation.persistence import knowledge_summaries as summaries_repo
from backend.conversation.persistence import knowledge_summary_generations as generations_repo
from backend.conversation.persistence import messages as messages_repo
from backend.conversation.persistence import threads as threads_repo
from backend.conversation.persistence import turns as turns_repo
from backend.conversation.services.knowledge_summary_enqueue import (
    KnowledgeSummaryEnqueueRepairService,
)
from backend.conversation.services.knowledge_summary_generation import (
    KnowledgeSummaryGenerationService,
)
from backend.conversation.services.token_counter import TokenCounter, WhitespaceTokenizer
from backend.settings import Settings


class FakeGateway:
    """不连接真实 OpenAI 的确定性 Structured Output Gateway。"""

    model_name = "fake-knowledge-model"

    def __init__(self, assistant_message_id: UUID) -> None:
        self.assistant_message_id = assistant_message_id
        self.extract_calls = 0
        self.merge_calls = 0

    async def extract(
        self, request: Mapping[str, Any]
    ) -> tuple[KnowledgeExtractionResult, dict[str, int | None]]:
        self.extract_calls += 1
        return (
            KnowledgeExtractionResult(
                candidates=[
                    KnowledgeCandidate(
                        scope="math",
                        topic_group_title="圆锥曲线",
                        topic_title="椭圆离心率",
                        aliases=["离心率"],
                        confidence=0.95,
                        reusable_value="save",
                        items=[
                            CandidateItem.model_validate(
                                {
                                    "section": "formulas",
                                    "text": "椭圆离心率 e=c/a，其中 a>c>0。",
                                    "confidence": 0.95,
                                    "supports": [
                                        {
                                            "message_id": self.assistant_message_id,
                                            "quote": "椭圆离心率 e=c/a，其中 a>c>0。",
                                        }
                                    ],
                                }
                            )
                        ],
                    )
                ]
            ),
            {"input_tokens": 10, "output_tokens": 20, "latency_ms": 1},
        )

    async def merge_plan(
        self, request: Mapping[str, Any]
    ) -> tuple[KnowledgeMergePlanResult, dict[str, int | None]]:
        self.merge_calls += 1
        return (
            KnowledgeMergePlanResult(
                plans=[
                    CreateSummaryPlan(
                        candidate_index=0,
                        match_confidence=0.10,
                        reason="没有现有总结命中",
                    )
                ]
            ),
            {"input_tokens": 20, "output_tokens": 20, "latency_ms": 1},
        )


class ReplanningGateway(FakeGateway):
    """首轮 create 后模拟并发已创建，下一轮根据最新 recall 返回 merge。"""

    def __init__(self, assistant_message_id: UUID) -> None:
        super().__init__(assistant_message_id)
        self._merge_target_id: UUID | None = None
        self._merge_target_version: int | None = None

    async def merge_plan(
        self, request: Mapping[str, Any]
    ) -> tuple[KnowledgeMergePlanResult, dict[str, int | None]]:
        self.merge_calls += 1
        recalled = request.get("existing_summary_candidates", [])
        if recalled:
            target = recalled[0]
            self._merge_target_id = UUID(str(target["summary_id"]))
            self._merge_target_version = int(target["version"])
            return (
                KnowledgeMergePlanResult(
                    plans=[
                        MergeSummaryPlan(
                            candidate_index=0,
                            target_summary_id=self._merge_target_id,
                            target_version=self._merge_target_version,
                            match_confidence=0.95,
                            item_mutations=[
                                AppendItemMutation(candidate_item_index=0, reason="并发创建后合并")
                            ],
                            reason="同主题总结已由并发 Job 创建",
                        )
                    ]
                ),
                {"input_tokens": 20, "output_tokens": 20, "latency_ms": 1},
            )
        return (
            KnowledgeMergePlanResult(
                plans=[
                    CreateSummaryPlan(
                        candidate_index=0,
                        match_confidence=0.10,
                        reason="没有现有总结命中",
                    )
                ]
            ),
            {"input_tokens": 20, "output_tokens": 20, "latency_ms": 1},
        )


class SlowGateway(FakeGateway):
    """通过可控等待模拟长模型调用，验证 Worker 运行期间会续租。"""

    def __init__(self, assistant_message_id: UUID) -> None:
        super().__init__(assistant_message_id)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def extract(
        self, request: Mapping[str, Any]
    ) -> tuple[KnowledgeExtractionResult, dict[str, int | None]]:
        self.started.set()
        await self.release.wait()
        return await super().extract(request)


def _config(*, lease_seconds: int = 60) -> SimpleNamespace:
    return SimpleNamespace(
        conversation_knowledge_summary_enabled=True,
        conversation_knowledge_summary_generation_enabled=True,
        conversation_knowledge_summary_auto_confidence=0.75,
        conversation_knowledge_summary_manual_confidence=0.60,
        conversation_knowledge_summary_context_messages=6,
        conversation_knowledge_summary_context_token_budget=4000,
        conversation_knowledge_summary_max_attempts=5,
        conversation_knowledge_summary_lease_seconds=lease_seconds,
    )


@pytest.mark.asyncio
async def test_generation_worker_fake_gateway_creates_summary(
    conversation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id = uuid4()
    thread_id = uuid4()
    turn_id = uuid4()
    user_message_id = uuid4()
    assistant_message_id = uuid4()
    occurred_at = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
    checkpoint = "conv-src-v1:test"

    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
            await turns_repo.insert_turn(
                session,
                turn_id=turn_id,
                thread_id=thread_id,
                user_id=user_id,
                client_request_id=f"worker-{turn_id}",
                request_id=f"request-{turn_id}",
                run_id=f"run-{turn_id}",
                user_message_id=user_message_id,
                expected_thread_version=0,
                graph_thread_id=str(thread_id),
                next_attempt_at=occurred_at,
            )
            await messages_repo.insert_message(
                session,
                message_id=user_message_id,
                thread_id=thread_id,
                turn_id=turn_id,
                user_id=user_id,
                sequence=1,
                role="user",
                content="请说明椭圆离心率。",
                content_hash=sha256("请说明椭圆离心率。".encode()).hexdigest(),
                occurred_at=occurred_at,
                completed_at=occurred_at,
            )
            answer = "椭圆离心率 e=c/a，其中 a>c>0。"
            await messages_repo.insert_message(
                session,
                message_id=assistant_message_id,
                thread_id=thread_id,
                turn_id=turn_id,
                user_id=user_id,
                sequence=2,
                role="assistant",
                content=answer,
                content_hash=sha256(answer.encode()).hexdigest(),
                occurred_at=occurred_at,
                completed_at=occurred_at,
            )
            await session.execute(
                text(
                    """
                    UPDATE conversation.conversation_turns
                    SET status = 'completed', assistant_message_id = :assistant_message_id,
                        source_checkpoint_id = :checkpoint
                    WHERE turn_id = :turn_id
                    """
                ),
                {
                    "turn_id": turn_id,
                    "assistant_message_id": assistant_message_id,
                    "checkpoint": checkpoint,
                },
            )
            generation_id = uuid4()
            assert await generations_repo.insert_generation_job(
                session,
                generation_id=generation_id,
                idempotency_key=f"test:{generation_id}",
                client_request_id=None,
                user_id=user_id,
                thread_id=thread_id,
                turn_id=turn_id,
                source_checkpoint_id=checkpoint,
                trigger="manual",
                primary_turn_occurred_at=occurred_at,
            )

    async with conversation_session_factory() as session:
        async with session.begin():
            rows = await generations_repo.claim_generation_jobs(
                session,
                worker_id="test-worker",
                lease_seconds=60,
                max_concurrency=4,
                manual_reserved_slots=1,
            )
    assert len(rows) == 1

    gateway = FakeGateway(assistant_message_id)
    service = KnowledgeSummaryGenerationService(
        session_factory=conversation_session_factory,
        config=_config(),
        gateway=gateway,
        token_counter=TokenCounter(WhitespaceTokenizer()),
        worker_id="test-worker",
    )
    await service.execute(rows[0])

    async with conversation_session_factory() as session:
        job = (
            (
                await session.execute(
                    text(
                        "SELECT status, attempt_count, extraction_result, merge_plan_result "
                        "FROM conversation.knowledge_summary_generation_jobs "
                        "WHERE generation_id = :generation_id"
                    ),
                    {"generation_id": generation_id},
                )
            )
            .mappings()
            .one()
        )
        assert job["status"] == "succeeded"
        assert job["attempt_count"] == 1
        support = job["extraction_result"]["candidates"][0]["items"][0]["supports"][0]
        assert support["quote_hash"]
        assert "quote" not in support
        summary = (
            (
                await session.execute(
                    text(
                        "SELECT version, source_count, source_message_count, content "
                        "FROM conversation.knowledge_summaries WHERE user_id = :user_id"
                    ),
                    {"user_id": user_id},
                )
            )
            .mappings()
            .one()
        )
        assert summary["version"] == 1
        assert summary["source_count"] == 1
        assert summary["source_message_count"] == 1
        assert len(summary["content"]["formulas"]) == 1
        revisions = (
            (
                await session.execute(
                    text(
                        "SELECT mutation_type FROM conversation.knowledge_summary_revisions "
                        "WHERE summary_id = (SELECT summary_id "
                        "FROM conversation.knowledge_summaries WHERE user_id = :user_id)"
                    ),
                    {"user_id": user_id},
                )
            )
            .scalars()
            .all()
        )
        assert revisions == ["create"]
        calls = (
            await session.execute(
                text(
                    "SELECT purpose, status FROM conversation.knowledge_summary_model_calls "
                    "WHERE generation_id = :generation_id ORDER BY purpose"
                ),
                {"generation_id": generation_id},
            )
        ).all()
        assert [tuple(item) for item in calls] == [
            ("extract", "succeeded"),
            ("merge_plan", "succeeded"),
        ]

    assert gateway.extract_calls == 1
    assert gateway.merge_calls == 1


@pytest.mark.asyncio
async def test_enqueue_repair_service_creates_job_for_enqueue_failed_turn(
    conversation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """KnowledgeSummaryEnqueueRepairService 修复 enqueue_failed Turn 并创建 Generation Job。"""
    user_id = uuid4()
    thread_id = uuid4()
    turn_id = uuid4()
    user_message_id = uuid4()
    assistant_message_id = uuid4()
    occurred_at = datetime(2026, 8, 18, 8, 0, tzinfo=UTC)
    checkpoint = "conv-src-v1:repair"

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

    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
            await turns_repo.insert_turn(
                session,
                turn_id=turn_id,
                thread_id=thread_id,
                user_id=user_id,
                client_request_id=f"repair-{turn_id}",
                request_id=f"request-{turn_id}",
                run_id=f"run-{turn_id}",
                user_message_id=user_message_id,
                expected_thread_version=0,
                graph_thread_id=str(thread_id),
                next_attempt_at=occurred_at,
            )
            await messages_repo.insert_message(
                session,
                message_id=user_message_id,
                thread_id=thread_id,
                turn_id=turn_id,
                user_id=user_id,
                sequence=1,
                role="user",
                content="请说明椭圆离心率。",
                content_hash=sha256("请说明椭圆离心率。".encode()).hexdigest(),
                occurred_at=occurred_at,
                completed_at=occurred_at,
            )
            await messages_repo.insert_message(
                session,
                message_id=assistant_message_id,
                thread_id=thread_id,
                turn_id=turn_id,
                user_id=user_id,
                sequence=2,
                role="assistant",
                content="椭圆离心率 e=c/a，其中 a>c>0。",
                content_hash=sha256("椭圆离心率 e=c/a，其中 a>c>0。".encode()).hexdigest(),
                occurred_at=occurred_at + timedelta(seconds=1),
                completed_at=occurred_at + timedelta(seconds=1),
            )
            await session.execute(
                text(
                    """
                    UPDATE conversation.conversation_turns
                    SET status = 'completed', assistant_message_id = :assistant_message_id,
                        source_checkpoint_id = :checkpoint,
                        knowledge_summary_enqueue_status = 'enqueue_failed',
                        knowledge_summary_enqueue_attempts = 1,
                        knowledge_summary_enqueue_next_attempt_at = :next_attempt
                    WHERE turn_id = :turn_id
                    """
                ),
                {
                    "turn_id": turn_id,
                    "assistant_message_id": assistant_message_id,
                    "checkpoint": checkpoint,
                    "next_attempt": datetime.now(UTC) - timedelta(seconds=1),
                },
            )

    repair = KnowledgeSummaryEnqueueRepairService(settings=settings)
    now = datetime.now(UTC)
    async with conversation_session_factory() as session:
        async with session.begin():
            rows = await summaries_repo.claim_enqueue_failed_turns(session, now=now, batch_size=50)
            assert len(rows) == 1
            await repair.repair_turn(session, turn_row=rows[0])

    async with conversation_session_factory() as session:
        async with session.begin():
            turn = (
                (
                    await session.execute(
                        text(
                            "SELECT knowledge_summary_enqueue_status "
                            "FROM conversation.conversation_turns "
                            "WHERE turn_id = :turn_id"
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
    assert turn["knowledge_summary_enqueue_status"] == "enqueued"
    assert len(jobs) == 1
    assert jobs[0]["trigger"] == "auto"
    assert jobs[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_worker_needs_review_proposal_is_readable_by_detail_api(
    conversation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """真实 Worker 写入的 review proposal 必须符合详情 API 的公开结构。"""
    user_id = uuid4()
    thread_id = uuid4()
    turn_id = uuid4()
    now = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
            await turns_repo.insert_turn(
                session,
                turn_id=turn_id,
                thread_id=thread_id,
                user_id=user_id,
                client_request_id=f"review-{turn_id}",
                request_id=f"request-{turn_id}",
                run_id=f"run-{turn_id}",
                user_message_id=uuid4(),
                expected_thread_version=0,
                graph_thread_id=str(thread_id),
                next_attempt_at=now,
            )
            await session.execute(
                text(
                    """
                    UPDATE conversation.conversation_turns
                    SET status = 'completed', source_checkpoint_id = 'review-checkpoint'
                    WHERE turn_id = :turn_id
                    """
                ),
                {"turn_id": turn_id},
            )
            summary_id = uuid4()
            generation_id = uuid4()
            content = {
                "schema_version": 1,
                "overview": None,
                "definitions": [],
                "theorems": [],
                "formulas": [],
                "properties": [],
                "methods": [],
                "pitfalls": [],
            }
            await session.execute(
                text(
                    """
                    INSERT INTO conversation.knowledge_summaries (
                        summary_id, user_id, topic_group_title, topic_title,
                        normalized_topic_group, normalized_topic_title, content,
                        search_text, content_hash, state_hash
                    ) VALUES (
                        :summary_id, :user_id, '函数', '导数', '函数', '导数',
                        CAST(:content AS jsonb), '函数 导数', :hash, :state_hash
                    )
                    """
                ),
                {
                    "summary_id": summary_id,
                    "user_id": user_id,
                    "content": json.dumps(content, ensure_ascii=False),
                    "hash": "a" * 64,
                    "state_hash": "b" * 64,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO conversation.knowledge_summary_generation_jobs (
                        generation_id, idempotency_key, user_id, thread_id, turn_id,
                        source_checkpoint_id, trigger, status, primary_turn_occurred_at,
                        created_at, updated_at, completed_at
                    ) VALUES (
                        :generation_id, :idempotency_key, :user_id, :thread_id, :turn_id,
                        'review-checkpoint', 'manual', 'needs_review', :now,
                        :now, :now, :now
                    )
                    """
                ),
                {
                    "generation_id": generation_id,
                    "idempotency_key": f"review:{generation_id}",
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "now": now,
                },
            )
            await session.execute(
                text(
                    """
                    INSERT INTO conversation.knowledge_summary_reviews (
                        review_id, generation_id, summary_id, user_id, candidate_index,
                        reason_code, internal_reason, proposed_content
                    ) VALUES (
                        :review_id, :generation_id, :summary_id, :user_id, 0,
                        'CONTRADICTORY_CONTENT', '测试',
                        CAST(:proposed_content AS jsonb)
                    )
                    """
                ),
                {
                    "review_id": uuid4(),
                    "generation_id": generation_id,
                    "summary_id": summary_id,
                    "user_id": user_id,
                    "proposed_content": json.dumps(
                        {
                            "proposed_topic_title": "导数的新建议",
                            "proposed_sections": {"definitions": ["导数是函数变化率"]},
                        },
                        ensure_ascii=False,
                    ),
                },
            )
    # 通过已存在的测试 API fixture 直接验证公开映射；proposal 不含内部 overview 字段。
    from backend.conversation.services.knowledge_summary_service import KnowledgeSummaryService

    service = KnowledgeSummaryService(
        session_factory=conversation_session_factory,
        settings=SimpleNamespace(cursor_hmac_key="test-key"),
    )
    detail = await service.get_summary_detail(user_id=user_id, summary_id=summary_id)
    assert detail.pending_reviews[0].proposed_topic_title == "导数的新建议"
    assert detail.pending_reviews[0].proposed_sections == {"definitions": ["导数是函数变化率"]}


@pytest.mark.asyncio
async def test_identity_conflict_replans_and_merges_existing_summary(
    conversation_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发同主题 create 命中唯一索引后，清除旧计划并重新召回走 merge。"""
    user_id = uuid4()
    thread_id = uuid4()
    turn_id = uuid4()
    user_message_id = uuid4()
    assistant_message_id = uuid4()
    occurred_at = datetime(2026, 8, 19, 8, 0, tzinfo=UTC)
    checkpoint = "conv-src-v1:identity-conflict"

    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
            await turns_repo.insert_turn(
                session,
                turn_id=turn_id,
                thread_id=thread_id,
                user_id=user_id,
                client_request_id=f"identity-{turn_id}",
                request_id=f"request-{turn_id}",
                run_id=f"run-{turn_id}",
                user_message_id=user_message_id,
                expected_thread_version=0,
                graph_thread_id=str(thread_id),
                next_attempt_at=occurred_at,
            )
            for message_id, sequence, role, content in (
                (user_message_id, 1, "user", "请说明椭圆离心率。"),
                (assistant_message_id, 2, "assistant", "椭圆离心率 e=c/a，其中 a>c>0。"),
            ):
                await messages_repo.insert_message(
                    session,
                    message_id=message_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    user_id=user_id,
                    sequence=sequence,
                    role=role,
                    content=content,
                    content_hash=sha256(content.encode()).hexdigest(),
                    occurred_at=occurred_at,
                    completed_at=occurred_at,
                )
            await session.execute(
                text(
                    "UPDATE conversation.conversation_turns "
                    "SET status = 'completed', assistant_message_id = :assistant_message_id, "
                    "source_checkpoint_id = :checkpoint WHERE turn_id = :turn_id"
                ),
                {
                    "turn_id": turn_id,
                    "assistant_message_id": assistant_message_id,
                    "checkpoint": checkpoint,
                },
            )
            generation_id = uuid4()
            assert await generations_repo.insert_generation_job(
                session,
                generation_id=generation_id,
                idempotency_key=f"identity:{generation_id}",
                client_request_id=None,
                user_id=user_id,
                thread_id=thread_id,
                turn_id=turn_id,
                source_checkpoint_id=checkpoint,
                trigger="manual",
                primary_turn_occurred_at=occurred_at,
            )
            rows = await generations_repo.claim_generation_jobs(
                session,
                worker_id="identity-worker",
                lease_seconds=60,
                max_concurrency=1,
                manual_reserved_slots=1,
                now=datetime.now(UTC),
            )
    row = rows[0]
    original_create = summaries_repo.create_summary_snapshot
    inserted_concurrent = False

    async def create_with_concurrent_insert(*args: Any, **kwargs: Any) -> None:
        nonlocal inserted_concurrent
        if not inserted_concurrent:
            inserted_concurrent = True
            async with conversation_session_factory() as competing_session:
                async with competing_session.begin():
                    competing_kwargs = {**kwargs, "summary_id": uuid4()}
                    await original_create(competing_session, **competing_kwargs)
        await original_create(*args, **kwargs)

    monkeypatch.setattr(summaries_repo, "create_summary_snapshot", create_with_concurrent_insert)
    gateway = ReplanningGateway(assistant_message_id)
    service = KnowledgeSummaryGenerationService(
        session_factory=conversation_session_factory,
        config=_config(),
        gateway=gateway,
        token_counter=TokenCounter(WhitespaceTokenizer()),
        worker_id="identity-worker",
    )
    await service.execute(row)

    async with conversation_session_factory() as session:
        job = (
            (
                await session.execute(
                    text(
                        "SELECT status, last_error_code, merge_plan_result "
                        "FROM conversation.knowledge_summary_generation_jobs "
                        "WHERE generation_id = :generation_id"
                    ),
                    {"generation_id": generation_id},
                )
            )
            .mappings()
            .one()
        )
        assert job["status"] == "succeeded", dict(job)
        assert job["merge_plan_result"]["plans"][0]["action"] == "merge"
        count = (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM conversation.knowledge_summaries WHERE user_id = :user_id"
                ),
                {"user_id": user_id},
            )
        ).scalar_one()
        assert count == 1
    assert gateway.merge_calls == 2


@pytest.mark.asyncio
async def test_generation_service_renews_lease_during_long_gateway_call(
    conversation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """模型调用超过短 lease 时，心跳续租使同一 Job 不能被第二个 Worker 重领。"""
    user_id = uuid4()
    thread_id = uuid4()
    turn_id = uuid4()
    user_message_id = uuid4()
    assistant_message_id = uuid4()
    now = datetime.now(UTC)
    checkpoint = "conv-src-v1:renew"
    async with conversation_session_factory() as session:
        async with session.begin():
            await threads_repo.insert_thread(session, thread_id, user_id)
            await turns_repo.insert_turn(
                session,
                turn_id=turn_id,
                thread_id=thread_id,
                user_id=user_id,
                client_request_id=f"renew-{turn_id}",
                request_id=f"request-{turn_id}",
                run_id=f"run-{turn_id}",
                user_message_id=user_message_id,
                expected_thread_version=0,
                graph_thread_id=str(thread_id),
                next_attempt_at=now,
            )
            for message_id, sequence, role, content in (
                (user_message_id, 1, "user", "请说明椭圆离心率。"),
                (assistant_message_id, 2, "assistant", "椭圆离心率 e=c/a，其中 a>c>0。"),
            ):
                await messages_repo.insert_message(
                    session,
                    message_id=message_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    user_id=user_id,
                    sequence=sequence,
                    role=role,
                    content=content,
                    content_hash=sha256(content.encode()).hexdigest(),
                    occurred_at=now,
                    completed_at=now,
                )
            await session.execute(
                text(
                    "UPDATE conversation.conversation_turns "
                    "SET status = 'completed', assistant_message_id = :assistant_message_id, "
                    "source_checkpoint_id = :checkpoint WHERE turn_id = :turn_id"
                ),
                {
                    "turn_id": turn_id,
                    "assistant_message_id": assistant_message_id,
                    "checkpoint": checkpoint,
                },
            )
            generation_id = uuid4()
            assert await generations_repo.insert_generation_job(
                session,
                generation_id=generation_id,
                idempotency_key=f"renew:{generation_id}",
                client_request_id=None,
                user_id=user_id,
                thread_id=thread_id,
                turn_id=turn_id,
                source_checkpoint_id=checkpoint,
                trigger="manual",
                primary_turn_occurred_at=now,
            )
            rows = await generations_repo.claim_generation_jobs(
                session,
                worker_id="renew-worker",
                lease_seconds=1,
                max_concurrency=1,
                manual_reserved_slots=1,
                now=datetime.now(UTC),
            )
    gateway = SlowGateway(assistant_message_id)
    service = KnowledgeSummaryGenerationService(
        session_factory=conversation_session_factory,
        config=_config(lease_seconds=1),
        gateway=gateway,
        token_counter=TokenCounter(WhitespaceTokenizer()),
        worker_id="renew-worker",
    )
    running = asyncio.create_task(service.execute(rows[0]))
    await asyncio.wait_for(gateway.started.wait(), timeout=2)
    await asyncio.sleep(0.5)
    async with conversation_session_factory() as session:
        async with session.begin():
            reclaimed = await generations_repo.claim_generation_jobs(
                session,
                worker_id="other-worker",
                lease_seconds=1,
                max_concurrency=1,
                manual_reserved_slots=1,
                now=datetime.now(UTC),
            )
    assert reclaimed == []
    gateway.release.set()
    await asyncio.wait_for(running, timeout=4)
