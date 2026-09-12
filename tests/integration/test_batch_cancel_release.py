"""批次取消语义与 sweep 分页的集成测试（review I-8 / 调度项）：真实 PostgreSQL。

三件只有真库能证明的事：

1. API/管理路径的 `request_cancel` 取消**批次** operation 时，在同一事务里释放成员
   （`batch_operation_id=NULL`、状态保持 `pending_batch`），成员回到证据池、能被下一个
   批次正常领走——旧实现只把批次置 `cancelled`，成员永久搁浅在
   `pending_batch AND batch_operation_id IS NOT NULL`；
2. 被用户取消的单条证据（`request_cancel` 明确允许取消 `pending_batch`）**绝不会**被
   任何批次写进长期记忆：`list_batch_member_operations` 只返回可写成员；
3. `list_open_runs` 的当天过滤已下推 SQL：历史残留 running run 再多也不会挤占当天席位
   （旧实现先 `created_at ASC LIMIT` 再在 Python 里筛）。

运行：`scripts/ci-local.sh backend-integration`，或显式注入 memory_test：
    DATABASE_URL=postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test \
    uv run pytest tests/integration/test_batch_cancel_release.py -q
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import (
    MasteryPatch,
    MutationPlanDraft,
    SummarizeUserMemoryBatchCommand,
)
from backend.memory.contracts.evidence import ConversationEvidence
from backend.memory.graph.llm_schemas import (
    CandidateExtractionResult,
    CandidateMemory,
    ExtractedEvidence,
    MutationPlanResult,
)
from backend.memory.graph.openai_client import FakeMemoryLLMClient
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.persistence import maintenance as maintenance_repo
from backend.memory.persistence import operations as ops_repo
from backend.memory.readers.testing import FakeConversationReader
from backend.memory.services.memory_service import MemoryService
from tests.integration.graph_helpers import make_operation, persist_operation

USER = UUID("00000000-0000-4000-8000-000000000051")
OTHER_USER = UUID("00000000-0000-4000-8000-000000000052")
#: 证据的沉淀到点时刻取过去，保证扫描条件 `next_run_at <= now()` 成立
ELIGIBLE_AT = datetime(2026, 9, 11, 0, 0, tzinfo=UTC)
TODAY = "2026-09-12"
MAINTENANCE_TYPE = "summarize_pending_evidence"


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _payload_hash(operation: object) -> str:
    from backend.memory.contracts.common import idempotency_payload_hash

    return idempotency_payload_hash(
        operation.payload.model_dump(mode="json")  # type: ignore[attr-defined]
    )


async def _persist_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID = USER,
    thread_id: str,
) -> object:
    """写一条待入批证据（`pending_batch` + 已到点的沉淀门控）。"""
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
                status="pending_batch",
                next_run_at=ELIGIBLE_AT,
            )
    return operation


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


async def _get(session_factory: async_sessionmaker[AsyncSession], operation_id: UUID) -> dict:
    async with session_factory() as session:
        row = await ops_repo.get_operation(session, operation_id)
    assert row is not None
    return dict(row)


async def _count(session_factory: async_sessionmaker[AsyncSession], table: str) -> int:
    async with session_factory() as session:
        result = await session.execute(text(f"SELECT count(*) FROM {table}"))
        return int(result.scalar_one())


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


def _queue_member(fake_llm: FakeMemoryLLMClient, topic: str) -> None:
    fake_llm.extract_queue.append(CandidateExtractionResult(candidates=[_candidate(topic)]))
    fake_llm.plan_queue.append(MutationPlanResult(plans=[_draft(topic)]))


async def _seed_source(reader: FakeConversationReader, thread_id: str, content: str) -> None:
    from backend.memory.contracts.evidence import SourceItem

    reader.add_message(
        thread_id,
        SourceItem(source_ref="m1", role="user", content=content, occurred_at=ELIGIBLE_AT),
    )


# ---------------------------------------------------------------------------
# 1. 取消批次 → 成员被释放回池子、可被下一个批次领走
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_cancel_batch_releases_members_to_pool(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await _persist_evidence(session_factory, thread_id="t-cancel-batch-1")
    second = await _persist_evidence(session_factory, thread_id="t-cancel-batch-2")
    batch = await _build_batch(
        session_factory,
        user_id=USER,
        member_ids=[first.operation_id, second.operation_id],  # type: ignore[attr-defined]
    )

    # 走 API/管理路径的取消（不是直接调 settle_batch_members）
    async with session_factory() as session:
        async with session.begin():
            cancelled = await ops_repo.request_cancel(
                session,
                operation_id=batch.operation_id,  # type: ignore[attr-defined]
            )
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"

    # 同一事务释放成员：状态保持 pending_batch、归属清空（回到证据池）
    for member in (first, second):
        row = await _get(session_factory, member.operation_id)  # type: ignore[attr-defined]
        assert row["status"] == "pending_batch", "取消批次不是处理完证据，不能把它置终态"
        assert row["batch_operation_id"] is None

    # 回到池子：夜间扫描再次看见该用户，且下一个批次能正常领走全部成员
    async with session_factory() as session:
        user_ids = await ops_repo.list_pending_batch_user_ids(
            session, now=datetime.now(UTC), limit=10
        )
        pool = await ops_repo.list_pending_batch_members(
            session, user_id=USER, now=datetime.now(UTC), limit=10
        )
    assert USER in user_ids
    assert {UUID(str(row["operation_id"])) for row in pool} == {
        first.operation_id,  # type: ignore[attr-defined]
        second.operation_id,  # type: ignore[attr-defined]
    }

    # 第二个批次正常领走（assign 的 rowcount 必须等于成员数）
    second_batch = await _build_batch(
        session_factory,
        user_id=USER,
        member_ids=[first.operation_id, second.operation_id],  # type: ignore[attr-defined]
    )
    for member in (first, second):
        row = await _get(session_factory, member.operation_id)  # type: ignore[attr-defined]
        assert row["batch_operation_id"] == second_batch.operation_id  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cancelled_batch_release_is_idempotent_for_non_batch_operation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """普通 operation 的取消不受影响：没有成员可释放，状态照常置 cancelled。"""
    evidence = await _persist_evidence(session_factory, thread_id="t-plain-cancel")

    async with session_factory() as session:
        async with session.begin():
            row = await ops_repo.request_cancel(
                session,
                operation_id=evidence.operation_id,  # type: ignore[attr-defined]
            )

    assert row is not None
    assert row["status"] == "cancelled"
    # 取消单条证据不影响别人的池子（它自己也不再出现在待入批扫描里）
    async with session_factory() as session:
        pool = await ops_repo.list_pending_batch_members(
            session, user_id=USER, now=datetime.now(UTC), limit=10
        )
    assert pool == []


# ---------------------------------------------------------------------------
# 2. 被取消的成员绝不进批次处理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_member_is_never_processed_by_a_batch(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
    memory_service: MemoryService,
) -> None:
    cancelled_evidence = await _persist_evidence(session_factory, thread_id="t-cancelled")
    kept_evidence = await _persist_evidence(session_factory, thread_id="t-kept")
    batch = await _build_batch(
        session_factory,
        user_id=USER,
        member_ids=[
            cancelled_evidence.operation_id,  # type: ignore[attr-defined]
            kept_evidence.operation_id,  # type: ignore[attr-defined]
        ],
    )
    # 用户取消其中一条证据（§11.6 明确允许取消 pending_batch）
    async with session_factory() as session:
        async with session.begin():
            row = await ops_repo.request_cancel(
                session,
                operation_id=cancelled_evidence.operation_id,  # type: ignore[attr-defined]
            )
    assert row is not None and row["status"] == "cancelled"

    # 仓储层过滤：批次只看到可写成员（cancelled 行即使仍带归属也不返回）
    async with session_factory() as session:
        visible = await ops_repo.list_batch_member_operations(
            session,
            batch_operation_id=batch.operation_id,  # type: ignore[attr-defined]
        )
    assert [UUID(str(item["operation_id"])) for item in visible] == [
        kept_evidence.operation_id  # type: ignore[attr-defined]
    ]

    # 只为"活着的那条"排 LLM 队列：被取消的那条若被处理，队列会空 → 批次直接失败
    await _seed_source(fake_conversation_reader, "t-kept", "我用配方法解出方程")
    await _seed_source(fake_conversation_reader, "t-cancelled", "我理解了椭圆定义")
    _queue_member(fake_llm, "配方法")

    result = await runner.run(batch)  # type: ignore[arg-type]

    assert result.status == "succeeded"
    assert await _count(session_factory, "memory_commits") == 1
    assert await memory_service.get_mastery(user_id=USER, topic_key="配方法") is not None
    # 被取消证据的主题从未写进长期记忆
    assert await memory_service.get_mastery(user_id=USER, topic_key="椭圆定义") is None
    async with session_factory() as session:
        documents = await session.execute(
            text("SELECT memory_id FROM memory_documents WHERE memory_id LIKE 'mastery:%'")
        )
        assert [row[0] for row in documents.all()] == ["mastery:配方法"]

    # 走真实 Worker 的终态路径（claim → complete）：批次 succeeded 只回写**可写成员**，
    # 被用户取消的那条必须保持 cancelled，不能被批次收尾"复活"成 succeeded。
    async with session_factory() as session:
        async with session.begin():
            claimed = await ops_repo.claim_operation(
                session, worker_id="test-worker", lease_seconds=60, batch_size=10
            )
    claimed_batch = next(
        row
        for row in claimed
        if row["operation_id"] == batch.operation_id  # type: ignore[attr-defined]
    )
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
    assert (await _get(session_factory, kept_evidence.operation_id))["status"] == "succeeded"  # type: ignore[attr-defined]
    assert (await _get(session_factory, cancelled_evidence.operation_id))["status"] == "cancelled"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_cancelled_member_does_not_block_sweep_of_its_own(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """取消单条证据后：批次仍在池子里看到别的成员，不会因为"空批次"被误判。"""
    member = await _persist_evidence(session_factory, thread_id="t-only-member")
    batch = await _build_batch(
        session_factory,
        user_id=USER,
        member_ids=[member.operation_id],  # type: ignore[attr-defined]
    )
    async with session_factory() as session:
        async with session.begin():
            await ops_repo.request_cancel(
                session,
                operation_id=member.operation_id,  # type: ignore[attr-defined]
            )

    # 批次仍然存在（未被取消），但它的成员集合为空 —— 图侧据此按 no_change 收尾，
    # 关键是**不会有任何成员被当成可写成员返回**。
    async with session_factory() as session:
        visible = await ops_repo.list_batch_member_operations(
            session,
            batch_operation_id=batch.operation_id,  # type: ignore[attr-defined]
        )
        batch_row = await ops_repo.get_operation(session, batch.operation_id)  # type: ignore[attr-defined]
    assert visible == []
    assert batch_row is not None and batch_row["status"] == "queued"


# ---------------------------------------------------------------------------
# 3. list_open_runs：当天过滤下推 SQL
# ---------------------------------------------------------------------------


async def _create_running_run(
    session_factory: async_sessionmaker[AsyncSession], *, key: str, created_at: datetime
) -> UUID:
    """建一个 running 状态的维护 run，并显式指定 created_at 保证排序确定。"""
    run_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await maintenance_repo.create_or_reuse_run(
                session, run_id=run_id, maintenance_type=MAINTENANCE_TYPE, idempotency_key=key
            )
            await session.execute(
                text(
                    "UPDATE memory_maintenance_runs "
                    "SET status = 'running', created_at = :created_at, started_at = :created_at "
                    "WHERE run_id = :run_id"
                ),
                {"created_at": created_at, "run_id": run_id},
            )
    return run_id


@pytest.mark.asyncio
async def test_list_open_runs_suffix_filter_is_pushed_down(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """30 条历史残留 + limit=1：不过滤只会拿到残留，下推当天过滤必须拿到当天 run。"""
    base = datetime.now(UTC) - timedelta(days=60)
    for day in range(1, 31):
        await _create_running_run(
            session_factory,
            key=f"summarize:{uuid4()}:2026-07-{day:02d}",
            created_at=base + timedelta(days=day),
        )
    today_run = await _create_running_run(
        session_factory,
        key=f"summarize:{uuid4()}:{TODAY}",
        created_at=datetime.now(UTC),
    )

    async with session_factory() as session:
        filtered = await maintenance_repo.list_open_runs(
            session,
            maintenance_type=MAINTENANCE_TYPE,
            limit=1,
            idempotency_suffix=f":{TODAY}",
        )
        unfiltered = await maintenance_repo.list_open_runs(
            session, maintenance_type=MAINTENANCE_TYPE, limit=1
        )

    assert [row["run_id"] for row in filtered] == [today_run]
    # 旧语义（先 LIMIT 再在 Python 里筛）只会拿到最早的残留 run：当天 run 被永久挤占
    assert unfiltered[0]["run_id"] != today_run
    assert str(unfiltered[0]["idempotency_key"]).endswith("2026-07-01")


@pytest.mark.asyncio
async def test_list_open_runs_without_suffix_keeps_legacy_semantics(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """不传后缀（既有调用方）仍返回全部 running run，按 created_at 升序。"""
    older = await _create_running_run(
        session_factory,
        key=f"summarize:{uuid4()}:2026-08-01",
        created_at=datetime.now(UTC) - timedelta(days=2),
    )
    newer = await _create_running_run(
        session_factory,
        key=f"summarize:{uuid4()}:{TODAY}",
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    async with session_factory() as session:
        rows = await maintenance_repo.list_open_runs(
            session, maintenance_type=MAINTENANCE_TYPE, limit=10
        )
    assert [row["run_id"] for row in rows] == [older, newer]
