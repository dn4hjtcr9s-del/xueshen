"""批量总结分支集成测试（memory-rebuild §4.2 / §5.8 Phase 6）：真实 PG + Fake Reader/LLM。

覆盖 §5.8「Phase 6 验收」里只有跑真实图才能证明的部分：

- 一个批量 operation 逐条处理该用户本批全部证据，**每批最多 50 条**（用 2 条验证循环
  真的走完、结果按成员归属）；
- 成员提交绑定到**成员自己的 operation**（`memory_commits.operation_id`），因此批次
  重跑时已写入的成员被跳过、**不重复生成内容**；
- 批次终态在同一事务内回写成员状态（成功 → 成员 succeeded；dead_letter/needs_review
  → 成员 dead_letter；cancelled → 成员被释放回证据池）；
- 跨用户不混批（成员按 `batch_operation_id` 归属，别的用户的行绝不进来）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import (
    MasteryPatch,
    MutationPlanDraft,
    SummarizeUserMemoryBatchCommand,
)
from backend.memory.contracts.evidence import ConversationEvidence, SourceItem
from backend.memory.graph.llm_schemas import (
    CandidateExtractionResult,
    CandidateMemory,
    ExtractedEvidence,
    MutationPlanResult,
)
from backend.memory.graph.openai_client import FakeMemoryLLMClient
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.persistence import operations as ops_repo
from backend.memory.readers.testing import FakeConversationReader
from backend.memory.services.memory_service import MemoryService
from tests.integration.graph_helpers import make_operation, persist_operation

USER = UUID("00000000-0000-4000-8000-000000000031")
OTHER_USER = UUID("00000000-0000-4000-8000-000000000032")
NOW = datetime(2026, 9, 12, 0, 5, 0, tzinfo=UTC)


def _candidate(topic: str) -> CandidateMemory:
    return CandidateMemory(
        memory_type="mastery",
        topic_title=topic,
        category="understanding",
        summary=f"用户掌握了{topic}",
        long_term_value="save",
        confidence=0.9,
        evidence=[
            ExtractedEvidence(
                evidence_ref="m1",
                evidence_type="user_solution",
                summary="用户独立解答",
                strength=0.9,
            )
        ],
    )


def _draft(topic: str) -> MutationPlanDraft:
    return MutationPlanDraft(
        target_memory_type="mastery",
        topic_title=topic,
        action="create",
        mastery_patch=MasteryPatch(overview=f"{topic}已掌握", understood_to_add=[topic]),
        candidate_indexes=[0],
        reasoning_summary="创建新主题",
    )


async def _persist_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    thread_id: str,
    status: str = "pending_batch",
) -> object:
    """写一条证据 operation（默认落沉淀态，供批量入批）。"""
    operation = make_operation(
        user_id=user_id,
        actor_type="conversation_agent",
        input_kind="evidence",
        operation_type="conversation_evidence",
        priority=50,
        payload=ConversationEvidence(
            thread_id=thread_id, message_ids=["m1"], trigger="turn_boundary"
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            await ops_repo.insert_operation(
                session,
                operation,  # type: ignore[arg-type]
                idempotency_payload_hash=_payload_hash(operation),
                status=status,
            )
    return operation


def _payload_hash(operation: object) -> str:
    from backend.memory.contracts.common import idempotency_payload_hash

    return idempotency_payload_hash(
        operation.payload.model_dump(mode="json")  # type: ignore[attr-defined]
    )


async def _build_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    member_ids: list[UUID],
) -> object:
    """建批次 operation 并把成员归属到它（模拟 Scheduler 的入批动作）。"""
    batch_operation_id = uuid4()
    batch = make_operation(
        user_id=user_id,
        actor_type="system",
        input_kind="evidence",
        operation_type="summarize_user_memory_batch",
        priority=50,
        payload=SummarizeUserMemoryBatchCommand(
            target_user_id=user_id,
            batch_operation_id=batch_operation_id,
            member_operation_ids=member_ids,
            max_evidence=50,
        ),
    )
    # make_operation 自己生成了 operation_id；payload 里的 batch_operation_id 必须与它一致
    batch = batch.model_copy(update={"operation_id": batch_operation_id})
    await persist_operation(session_factory, batch)  # type: ignore[arg-type]
    async with session_factory() as session:
        async with session.begin():
            assigned = await ops_repo.assign_batch_members(
                session,
                batch_operation_id=batch_operation_id,
                member_operation_ids=member_ids,
            )
    assert assigned == len(member_ids)
    return batch


async def _count(session_factory: async_sessionmaker[AsyncSession], table: str) -> int:
    async with session_factory() as session:
        result = await session.execute(text(f"SELECT count(*) FROM {table}"))
        return int(result.scalar_one())


async def _count_mastery_documents(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """只数 mastery 文档：每个用户还有一个 index 文档（+ 可能的 learner），不计入。"""
    async with session_factory() as session:
        result = await session.execute(
            text("SELECT count(*) FROM memory_documents WHERE memory_id LIKE 'mastery:%'")
        )
        return int(result.scalar_one())


async def _status_of(session_factory: async_sessionmaker[AsyncSession], operation_id: UUID) -> str:
    async with session_factory() as session:
        row = await ops_repo.get_operation(session, operation_id)
    assert row is not None
    return str(row["status"])


async def _seed_source(reader: FakeConversationReader, thread_id: str, content: str) -> None:
    reader.add_message(
        thread_id,
        SourceItem(source_ref="m1", role="user", content=content, occurred_at=NOW),
    )


def _queue_member(fake_llm: FakeMemoryLLMClient, topic: str) -> None:
    """为一条成员排队一次抽取 + 一次计划（summary 链每成员各消费一次）。"""
    fake_llm.extract_queue.append(CandidateExtractionResult(candidates=[_candidate(topic)]))
    fake_llm.plan_queue.append(MutationPlanResult(plans=[_draft(topic)]))


@pytest.mark.asyncio
async def test_batch_processes_every_member_and_binds_commits_to_members(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    """两条证据 → 两条成员提交，且每条提交绑定到成员自己的 operation。"""
    first = await _persist_evidence(session_factory, user_id=USER, thread_id="t-batch-1")
    second = await _persist_evidence(session_factory, user_id=USER, thread_id="t-batch-2")
    await _seed_source(fake_conversation_reader, "t-batch-1", "我用配方法解出方程")
    await _seed_source(fake_conversation_reader, "t-batch-2", "我理解了椭圆定义")
    _queue_member(fake_llm, "配方法")
    _queue_member(fake_llm, "椭圆定义")

    batch = await _build_batch(
        session_factory,
        user_id=USER,
        member_ids=[first.operation_id, second.operation_id],  # type: ignore[attr-defined]
    )
    result = await runner.run(batch)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    assert len(result.mutations) == 2
    assert await _count_mastery_documents(session_factory) == 2
    assert await _count(session_factory, "memory_commits") == 2
    # 提交绑定到成员 operation，而不是批次 operation：批次重跑才能靠它幂等
    async with session_factory() as session:
        rows = await session.execute(
            text("SELECT DISTINCT operation_id FROM memory_commits WHERE user_id = :u"),
            {"u": USER},
        )
        commit_operations = {row[0] for row in rows.all()}
    assert commit_operations == {first.operation_id, second.operation_id}  # type: ignore[attr-defined]
    assert await memory_service.get_mastery(user_id=USER, topic_key="配方法") is not None
    assert await memory_service.get_mastery(user_id=USER, topic_key="椭圆定义") is not None


@pytest.mark.asyncio
async def test_rerunning_batch_does_not_duplicate_content(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    """checkpoint 丢失后整批重跑：已提交成员被跳过，不重复生成内容。"""
    first = await _persist_evidence(session_factory, user_id=USER, thread_id="t-rerun-1")
    second = await _persist_evidence(session_factory, user_id=USER, thread_id="t-rerun-2")
    await _seed_source(fake_conversation_reader, "t-rerun-1", "我用配方法解出方程")
    await _seed_source(fake_conversation_reader, "t-rerun-2", "我理解了椭圆定义")
    _queue_member(fake_llm, "配方法")
    _queue_member(fake_llm, "椭圆定义")
    batch = await _build_batch(
        session_factory,
        user_id=USER,
        member_ids=[first.operation_id, second.operation_id],  # type: ignore[attr-defined]
    )

    first_run = await runner.run(batch)  # type: ignore[arg-type]
    assert len(first_run.mutations) == 2

    # 清掉 LLM 队列：重跑时若还要抽取，会因队列为空直接报错——这本身就是断言
    fake_llm.extract_queue.clear()
    fake_llm.plan_queue.clear()
    second_run = await runner.run(batch)  # type: ignore[arg-type]

    assert second_run.mutations == []
    assert await _count(session_factory, "memory_commits") == 2
    assert await _count_mastery_documents(session_factory) == 2
    # 两个成员都被记为"已提交跳过"，且批次仍算成功（没有任何东西需要再写）
    assert second_run.status == "succeeded"


@pytest.mark.asyncio
async def test_batch_terminal_state_settles_members(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    """批次 succeeded → 成员 succeeded（同一事务，成员状态与父批次一致）。"""
    first = await _persist_evidence(session_factory, user_id=USER, thread_id="t-settle-1")
    second = await _persist_evidence(session_factory, user_id=USER, thread_id="t-settle-2")
    await _seed_source(fake_conversation_reader, "t-settle-1", "我用配方法解出方程")
    await _seed_source(fake_conversation_reader, "t-settle-2", "我理解了椭圆定义")
    _queue_member(fake_llm, "配方法")
    _queue_member(fake_llm, "椭圆定义")
    batch = await _build_batch(
        session_factory,
        user_id=USER,
        member_ids=[first.operation_id, second.operation_id],  # type: ignore[attr-defined]
    )
    await runner.run(batch)  # type: ignore[arg-type]

    # 走真实 Worker 的终态路径：claim → complete（fencing CAS）
    async with session_factory() as session:
        async with session.begin():
            claimed = await ops_repo.claim_operation(
                session, worker_id="test-worker", lease_seconds=60, batch_size=1
            )
    claimed_batch = next(row for row in claimed if row["operation_id"] == batch.operation_id)  # type: ignore[attr-defined]
    async with session_factory() as session:
        async with session.begin():
            written = await ops_repo.complete_operation(
                session,
                operation_id=batch.operation_id,  # type: ignore[attr-defined]
                status="succeeded",
                result={"mutations": []},
                public_error=None,
                expected_worker="test-worker",
                expected_generation=int(claimed_batch["lease_generation"]),
            )
    assert written is True

    assert await _status_of(session_factory, first.operation_id) == "succeeded"  # type: ignore[attr-defined]
    assert await _status_of(session_factory, second.operation_id) == "succeeded"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_batch_dead_letter_marks_members_dead_letter(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """批次 dead_letter → 成员一并 dead_letter（保留批次归属供人工排查）。"""
    first = await _persist_evidence(session_factory, user_id=USER, thread_id="t-dl-1")
    batch = await _build_batch(
        session_factory,
        user_id=USER,
        member_ids=[first.operation_id],  # type: ignore[attr-defined]
    )
    async with session_factory() as session:
        async with session.begin():
            await ops_repo.claim_operation(
                session, worker_id="test-worker", lease_seconds=60, batch_size=1
            )
    async with session_factory() as session:
        async with session.begin():
            row = await ops_repo.get_operation(session, batch.operation_id)  # type: ignore[attr-defined]
            assert row is not None
            await ops_repo.complete_operation(
                session,
                operation_id=batch.operation_id,  # type: ignore[attr-defined]
                status="dead_letter",
                result=None,
                public_error={"code": "INTERNAL_ERROR", "message": "boom"},
                expected_worker="test-worker",
                expected_generation=int(row["lease_generation"]),
            )

    assert await _status_of(session_factory, first.operation_id) == "dead_letter"  # type: ignore[attr-defined]
    async with session_factory() as session:
        member = await ops_repo.get_operation(session, first.operation_id)  # type: ignore[attr-defined]
    assert member is not None and member["batch_operation_id"] == batch.operation_id  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_batch_cancel_releases_members(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """批次 cancelled → 成员回到证据池（状态不变、归属清空），可以被下一批重新领走。"""
    first = await _persist_evidence(session_factory, user_id=USER, thread_id="t-cancel-1")
    batch = await _build_batch(
        session_factory,
        user_id=USER,
        member_ids=[first.operation_id],  # type: ignore[attr-defined]
    )
    async with session_factory() as session:
        async with session.begin():
            released = await ops_repo.settle_batch_members(
                session,
                batch_operation_id=batch.operation_id,
                status="cancelled",  # type: ignore[attr-defined]
            )
    assert released == 1
    async with session_factory() as session:
        member = await ops_repo.get_operation(session, first.operation_id)  # type: ignore[attr-defined]
    assert member is not None
    assert member["status"] == "pending_batch"
    assert member["batch_operation_id"] is None


@pytest.mark.asyncio
async def test_other_users_evidence_is_not_dragged_into_the_batch(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    """跨用户不混批：只处理归属到本批次的成员。"""
    mine = await _persist_evidence(session_factory, user_id=USER, thread_id="t-mine")
    theirs = await _persist_evidence(session_factory, user_id=OTHER_USER, thread_id="t-theirs")
    await _seed_source(fake_conversation_reader, "t-mine", "我用配方法解出方程")
    await _seed_source(fake_conversation_reader, "t-theirs", "别人不该被处理")
    _queue_member(fake_llm, "配方法")

    batch = await _build_batch(session_factory, user_id=USER, member_ids=[mine.operation_id])  # type: ignore[attr-defined]
    result = await runner.run(batch)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    assert len(result.mutations) == 1
    # 另一个用户的证据既没被处理、也没被归属
    async with session_factory() as session:
        other = await ops_repo.get_operation(session, theirs.operation_id)  # type: ignore[attr-defined]
    assert other is not None
    assert other["batch_operation_id"] is None
    assert other["status"] == "pending_batch"
    assert fake_llm.extract_queue == []


@pytest.mark.asyncio
async def test_batch_processes_more_members_than_the_per_operation_llm_budget(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    """回归：成员数超过单 operation 的 LLM 预算（4 次 = 2 条成员）时仍要全部处理。

    `LLMCallBudget` 的上限是"每 operation 4 次"（extract + plan 各一次/成员），而批量的
    每个成员本身就是一条 operation。曾把预算跨成员累计 → 第 3 条成员起全部被判
    "LLM 调用预算耗尽"，3 条只写出 2 条，**且批次仍报 succeeded**。
    """
    member_ids = []
    for index in range(3):
        thread = f"t-budget-{index}"
        member = await _persist_evidence(session_factory, user_id=USER, thread_id=thread)
        await _seed_source(fake_conversation_reader, thread, f"我用配方法解出方程 {index}")
        _queue_member(fake_llm, f"预算主题{index}")
        member_ids.append(member.operation_id)  # type: ignore[attr-defined]

    batch = await _build_batch(session_factory, user_id=USER, member_ids=member_ids)
    result = await runner.run(batch)  # type: ignore[arg-type]

    assert len(result.mutations) == 3, "三条成员都必须写出，不能因预算被累计而丢"
    assert await _count(session_factory, "memory_commits") == 3
    assert not any("预算耗尽" in warning for warning in result.warnings), result.warnings
