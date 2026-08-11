"""Graph 分支集成测试（§23.2 / §23.3）：真实 PG + Fake Reader/LLM + 真实服务。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import (
    CorrectMemoryCommand,
    ForgetMemoryCommand,
    GraphStateCommand,
    MasteryReplacement,
    MutationPlanDraft,
    RestoreMemoryCommand,
)
from backend.memory.contracts.evidence import (
    ActivityEvidence,
    ConversationEvidence,
    SourceItem,
)
from backend.memory.graph.llm_schemas import (
    CandidateExtractionResult,
    CandidateMemory,
    ExtractedEvidence,
    MutationPlanResult,
)
from backend.memory.graph.openai_client import FakeMemoryLLMClient
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.readers.testing import (
    FakeActivityReader,
    FakeConversationReader,
)
from backend.memory.services.memory_service import MemoryService
from tests.integration.graph_helpers import make_operation, persist_operation

USER = UUID("00000000-0000-4000-8000-000000000021")
NOW = datetime(2026, 8, 11, 8, 0, 0, tzinfo=UTC)


def _conv_operation() -> object:
    return make_operation(
        user_id=USER,
        actor_type="conversation_agent",
        input_kind="evidence",
        operation_type="conversation_evidence",
        priority=50,
        payload=ConversationEvidence(thread_id="t1", message_ids=["m1"], trigger="turn_boundary"),
    )


def _candidate(
    *, confidence: float = 0.9, long_term_value: str = "save", topic: str | None = "二次函数"
) -> CandidateMemory:
    return CandidateMemory(
        memory_type="mastery" if topic else "learner",
        topic_title=topic,
        category="understanding" if topic else "goal",
        summary="用户独立用配方法解出方程",
        long_term_value=long_term_value,  # type: ignore[arg-type]
        confidence=confidence,
        evidence=[
            ExtractedEvidence(
                evidence_ref="m1",
                evidence_type="user_solution",
                summary="用户独立解答",
                strength=0.9,
            )
        ],
    )


def _create_draft(topic: str = "二次函数") -> MutationPlanDraft:
    from backend.memory.contracts.commands import MasteryPatch

    return MutationPlanDraft(
        target_memory_type="mastery",
        topic_title=topic,
        action="create",
        mastery_patch=MasteryPatch(overview="能独立配方", understood_to_add=["配方法"]),
        candidate_indexes=[0],
        reasoning_summary="创建新主题",
    )


async def _seed_conversation(reader: FakeConversationReader) -> None:
    reader.add_message(
        "t1",
        SourceItem(
            source_ref="m1",
            role="user",
            content="我用配方法解出来了：x=-1 或 x=-5",
            occurred_at=NOW,
        ),
    )


async def _count(session_factory: async_sessionmaker[AsyncSession], table: str) -> int:
    async with session_factory() as session:
        result = await session.execute(text(f"SELECT count(*) FROM {table}"))
        return int(result.scalar_one())


async def test_no_long_term_value_no_commit(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    await _seed_conversation(fake_conversation_reader)
    fake_llm.extract_queue.append(
        CandidateExtractionResult(candidates=[_candidate(long_term_value="ignore")])
    )
    operation = _conv_operation()
    await persist_operation(session_factory, operation)  # type: ignore[arg-type]

    result = await runner.run(operation)  # type: ignore[arg-type]
    assert result.status == "succeeded"
    assert result.mutations == []
    assert await _count(session_factory, "memory_documents") == 0
    assert await _count(session_factory, "memory_commits") == 0
    # 无候选时不发起第 2 次 LLM 调用
    assert len(fake_llm.records) == 1


async def test_low_confidence_goes_to_review(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    await _seed_conversation(fake_conversation_reader)
    fake_llm.extract_queue.append(
        CandidateExtractionResult(candidates=[_candidate(confidence=0.6)])
    )
    operation = _conv_operation()
    await persist_operation(session_factory, operation)  # type: ignore[arg-type]

    result = await runner.run(operation)  # type: ignore[arg-type]
    assert result.status == "needs_review"
    assert len(result.review_candidate_ids) == 1
    assert await _count(session_factory, "memory_review_candidates") == 1
    # 低置信不写活动 Markdown
    assert await _count(session_factory, "memory_documents") == 0


async def test_invalid_candidate_indexes_rejected(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    await _seed_conversation(fake_conversation_reader)
    fake_llm.extract_queue.append(CandidateExtractionResult(candidates=[_candidate()]))
    draft = _create_draft().model_copy(update={"candidate_indexes": [7]})
    fake_llm.plan_queue.append(MutationPlanResult(plans=[draft]))
    operation = _conv_operation()
    await persist_operation(session_factory, operation)  # type: ignore[arg-type]

    result = await runner.run(operation)  # type: ignore[arg-type]
    assert result.mutations == []
    assert any("非法 candidate_indexes" in w for w in result.warnings)
    assert await _count(session_factory, "memory_commits") == 0


async def test_summary_without_graph_node_still_commits(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    await _seed_conversation(fake_conversation_reader)
    fake_llm.extract_queue.append(
        CandidateExtractionResult(candidates=[_candidate(topic="炒菜火候控制")])
    )
    fake_llm.plan_queue.append(MutationPlanResult(plans=[_create_draft("炒菜火候控制")]))
    operation = _conv_operation()
    await persist_operation(session_factory, operation)  # type: ignore[arg-type]

    result = await runner.run(operation)  # type: ignore[arg-type]
    assert result.status == "succeeded"
    assert len(result.mutations) == 1
    mastery = await memory_service.get_mastery(user_id=USER, topic_key="炒菜火候控制")
    assert mastery is not None and mastery.version == 1

    # 总结提交不直接改图谱；projection 由 Outbox Consumer 异步创建（§10.4）
    assert await _count(session_factory, "graph_user_states") == 0
    async with session_factory() as session:
        rows = await session.execute(
            text("SELECT event_type FROM memory_outbox WHERE user_id = :u"), {"u": USER}
        )
        event_types = {r[0] for r in rows.all()}
    assert "memory.changed" in event_types


async def test_graph_state_command_no_openai(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeMemoryLLMClient,
) -> None:
    async with session_factory() as session:
        row = await session.execute(text("SELECT node_id FROM knowledge_graph_nodes LIMIT 1"))
        node_id = str(row.scalar_one())
    operation = make_operation(
        user_id=USER,
        actor_type="knowledge_graph_ui",
        input_kind="command",
        operation_type="set_graph_state",
        priority=40,
        payload=GraphStateCommand(node_id=node_id, action="mark_familiar"),
    )
    await persist_operation(session_factory, operation)

    result = await runner.run(operation)
    assert result.status == "succeeded"
    assert len(result.graph_state_changes) == 1
    assert result.graph_state_changes[0].after_status == "proficient"
    # 用户命令分支不调用 OpenAI（§23.2）
    assert fake_llm.records == []
    # 无候选举证下不能产生 expert
    # 已有 Overlay 的更新必须携带 expected_version（乐观并发）
    operation2 = make_operation(
        user_id=USER,
        actor_type="knowledge_graph_ui",
        input_kind="command",
        operation_type="set_graph_state",
        priority=40,
        payload=GraphStateCommand(node_id=node_id, action="mark_familiar", expected_version=1),
    )
    await persist_operation(session_factory, operation2)
    result2 = await runner.run(operation2)
    assert result2.graph_state_changes[0].after_status == "proficient"


async def test_activity_exposure_idempotent(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    fake_activity_reader: FakeActivityReader,
    fake_llm: FakeMemoryLLMClient,
) -> None:
    async with session_factory() as session:
        row = await session.execute(text("SELECT node_id FROM knowledge_graph_nodes LIMIT 1"))
        node_id = str(row.scalar_one())

    def _exposure_op() -> object:
        return make_operation(
            user_id=USER,
            actor_type="activity_agent",
            input_kind="evidence",
            operation_type="activity_evidence",
            priority=60,
            payload=ActivityEvidence(
                activity_type="page_view",
                activity_ids=["pv-1"],
                aggregated_count=3,
                graph_node_hints=[node_id],
            ),
        )

    op1 = _exposure_op()
    await persist_operation(session_factory, op1)  # type: ignore[arg-type]
    result1 = await runner.run(op1)  # type: ignore[arg-type]
    assert result1.status == "succeeded"

    # 同一 activity_id 重投（新 operation）不重复计数（裁决 A）
    op2 = _exposure_op()
    await persist_operation(session_factory, op2)  # type: ignore[arg-type]
    await runner.run(op2)  # type: ignore[arg-type]

    async with session_factory() as session:
        row = await session.execute(
            text(
                "SELECT event_count FROM graph_user_node_activity "
                "WHERE user_id = :u AND node_id = :n"
            ),
            {"u": USER, "n": node_id},
        )
        assert int(row.scalar_one()) == 3
    # exposure 分支不调用 OpenAI
    assert fake_llm.records == []


async def test_activity_exposure_invalid_hint_no_change(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    operation = make_operation(
        user_id=USER,
        actor_type="activity_agent",
        input_kind="evidence",
        operation_type="activity_evidence",
        priority=60,
        payload=ActivityEvidence(
            activity_type="bookmark",
            activity_ids=["bm-1"],
            graph_node_hints=["n999999"],
        ),
    )
    await persist_operation(session_factory, operation)
    result = await runner.run(operation)
    assert result.status == "succeeded"
    assert await _count(session_factory, "graph_user_node_activity") == 0


async def test_correct_forget_restore_flow(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    # 先通过总结链路创建主题
    await _seed_conversation(fake_conversation_reader)
    fake_llm.extract_queue.append(CandidateExtractionResult(candidates=[_candidate()]))
    fake_llm.plan_queue.append(MutationPlanResult(plans=[_create_draft()]))
    op0 = _conv_operation()
    await persist_operation(session_factory, op0)  # type: ignore[arg-type]
    await runner.run(op0)  # type: ignore[arg-type]

    # correct_memory：确定性 replace，不调用 OpenAI
    correct = make_operation(
        user_id=USER,
        actor_type="user",
        input_kind="command",
        operation_type="correct_memory",
        priority=10,
        payload=CorrectMemoryCommand(
            memory_id="mastery:二次函数",
            expected_version=1,
            replacement=MasteryReplacement(
                topic_title="二次函数（已纠正）",
                overview="用户纠正后的描述",
                understood=["配方法", "求根公式"],
            ),
            reason="用户主动纠正",
        ),
    )
    await persist_operation(session_factory, correct)
    result = await runner.run(correct)
    assert result.status == "succeeded"
    assert result.mutations[0].after_version == 2
    assert len(fake_llm.records) == 2  # 仍是总结链路的 2 次调用

    # forget → tombstone
    forget = make_operation(
        user_id=USER,
        actor_type="user",
        input_kind="command",
        operation_type="forget_memory",
        priority=10,
        payload=ForgetMemoryCommand(
            memory_id="mastery:二次函数", expected_version=2, reason="删除"
        ),
    )
    await persist_operation(session_factory, forget)
    result = await runner.run(forget)
    assert result.mutations[0].action == "forget"
    assert await memory_service.get_mastery(user_id=USER, topic_key="二次函数") is None

    # restore → 递增新版本 v3
    restore = make_operation(
        user_id=USER,
        actor_type="user",
        input_kind="command",
        operation_type="restore_memory",
        priority=10,
        payload=RestoreMemoryCommand(memory_id="mastery:二次函数", deleted_version=2),
    )
    await persist_operation(session_factory, restore)
    result = await runner.run(restore)
    assert result.mutations[0].after_version == 3
    mastery = await memory_service.get_mastery(user_id=USER, topic_key="二次函数")
    assert mastery is not None and mastery.topic_title == "二次函数（已纠正）"
