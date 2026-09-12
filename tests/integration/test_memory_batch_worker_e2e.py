"""真实 Worker 驱动的批量总结端到端集成测试（评审 C-2 / I-9 的回归证据）。

本文件回答两个"只有真实执行层才会暴露"的问题：

1. **C-2 lease fencing**：现有测试全部用 ``runner.run(batch)``（不带 fencing），因此走的是
   ``mark_commit_started`` 的无 fencing 直调分支；真实 Worker 会用 claim 得到的
   ``(worker_id, lease_generation)`` 做 CAS，而批次里的**成员 operation 从未被 claim**
   （状态 ``pending_batch``、``locked_by`` 为空）——拿成员行做 CAS 恒 0 行 →
   ``LeaseFencedError`` → 批次永远写不进任何事实、成员永久搁浅。
   本文件走完整链路：``claim_operation → Worker._execute（内部 runner.run(fencing=...)）
   → complete_operation``，并在 PG 与 Markdown 两侧断言成员事实真的落盘。
2. **I-9 成员级失败隔离**：一条成员抛非预算类异常时，其余成员仍要写入、该成员记
   ``batch_failed``（带稳定 reason + 截断摘要）、批次整体仍能走到终态。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
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
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.graph import batch as batch_module
from backend.memory.graph.llm_schemas import (
    CandidateExtractionResult,
    CandidateMemory,
    ExtractedEvidence,
    MutationPlanResult,
)
from backend.memory.graph.openai_client import FakeMemoryLLMClient
from backend.memory.graph.policies import LLMBudgetExceededError
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.graph.state import MemoryRuntimeContext
from backend.memory.persistence import operations as ops_repo
from backend.memory.readers.testing import FakeConversationReader
from backend.memory.services.memory_service import MemoryService
from backend.memory.worker.worker import Worker, WorkerConfig
from backend.settings import Settings
from tests.integration.graph_helpers import make_operation, persist_operation

USER = UUID("00000000-0000-4000-8000-0000000000a1")
NOW = datetime(2026, 9, 12, 0, 5, 0, tzinfo=UTC)

WORKER_ID = "e2e-worker"


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


def _payload_hash(operation: MemoryOperation) -> str:
    from backend.memory.contracts.common import idempotency_payload_hash

    return idempotency_payload_hash(operation.payload.model_dump(mode="json"))


async def _persist_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    thread_id: str,
    next_run_at: datetime = NOW,
) -> MemoryOperation:
    """写一条 ``pending_batch`` 证据（与 Scheduler 入池路径一致：状态 + 门控时间）。

    ``next_run_at`` 同时决定批次内的成员序（``list_batch_member_operations`` 的排序键是
    next_run_at/created_at/operation_id），测试用**不同**的门控时间固定处理顺序，
    避免依赖随机 UUID 的排序。
    """
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
            inserted = await ops_repo.insert_operation(
                session,
                operation,
                idempotency_payload_hash=_payload_hash(operation),
                status=ops_repo.PENDING_BATCH_STATUS,
                next_run_at=next_run_at,
            )
    assert inserted is True
    return operation


async def _build_batch(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    member_ids: list[UUID],
) -> MemoryOperation:
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
    ).model_copy(update={"operation_id": batch_operation_id})
    await persist_operation(session_factory, batch)
    async with session_factory() as session:
        async with session.begin():
            assigned = await ops_repo.assign_batch_members(
                session, batch_operation_id=batch_operation_id, member_operation_ids=member_ids
            )
    assert assigned == len(member_ids)
    return batch


def _mastery_files(root: Path) -> list[Path]:
    """同步收集 mastery 文档文件（放线程里跑，避免在协程里做阻塞 IO）。"""
    return list(root.rglob("mastery/*.md"))


def _make_worker(
    session_factory: async_sessionmaker[AsyncSession], runner: LocalLangGraphRunner
) -> Worker:
    """真实 Worker（同一个执行入口：心跳/超时/终态写回全部按生产语义）。"""
    return Worker(
        session_factory=session_factory,
        runner=runner,
        worker_id=WORKER_ID,
        config=WorkerConfig(lease_seconds=60, poll_interval_seconds=0.01),
    )


async def _claim_and_execute(
    session_factory: async_sessionmaker[AsyncSession],
    worker: Worker,
    operation_id: UUID,
) -> dict[str, object]:
    """claim（拿 lease_generation）→ Worker._execute（内部带 fencing 跑图）→ 读回行。"""
    async with session_factory() as session:
        async with session.begin():
            claimed = await ops_repo.claim_operation(
                session,
                worker_id=WORKER_ID,
                lease_seconds=60,
                operation_id=operation_id,
            )
    assert len(claimed) == 1, "批次 operation 必须能被真实 claim 领取"
    # `_execute` 正是被测的真实执行入口（claim 之后的 fencing/心跳/终态写回全在内）
    await worker._execute(claimed[0])
    async with session_factory() as session:
        row = await ops_repo.get_operation(session, operation_id)
    assert row is not None
    return row


async def _operation_status(
    session_factory: async_sessionmaker[AsyncSession], operation_id: UUID
) -> str:
    async with session_factory() as session:
        row = await ops_repo.get_operation(session, operation_id)
    assert row is not None
    return str(row["status"])


def _queue_member(fake_llm: FakeMemoryLLMClient, topic: str) -> None:
    fake_llm.extract_queue.append(CandidateExtractionResult(candidates=[_candidate(topic)]))
    fake_llm.plan_queue.append(MutationPlanResult(plans=[_draft(topic)]))


@pytest.mark.asyncio
async def test_real_worker_writes_batch_member_facts_and_settles_statuses(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
    settings: Settings,
) -> None:
    """C-2 直接回归：真实 Worker（带 lease fencing）跑批次，成员事实必须真的写进去。"""
    first = await _persist_evidence(
        session_factory, user_id=USER, thread_id="t-e2e-1", next_run_at=NOW
    )
    second = await _persist_evidence(
        session_factory, user_id=USER, thread_id="t-e2e-2", next_run_at=NOW + timedelta(seconds=1)
    )
    fake_conversation_reader.add_message(
        "t-e2e-1",
        SourceItem(source_ref="m1", role="user", content="我用配方法解出方程", occurred_at=NOW),
    )
    fake_conversation_reader.add_message(
        "t-e2e-2",
        SourceItem(source_ref="m1", role="user", content="我理解了椭圆定义", occurred_at=NOW),
    )
    _queue_member(fake_llm, "配方法")
    _queue_member(fake_llm, "椭圆定义")
    batch = await _build_batch(
        session_factory, user_id=USER, member_ids=[first.operation_id, second.operation_id]
    )

    worker = _make_worker(session_factory, runner)
    row = await _claim_and_execute(session_factory, worker, batch.operation_id)

    # 1. 批次走到终态且没有被 fencing 拒绝（C-2 修复前这里恒为 running + 0 提交）
    assert row["status"] == "succeeded", row.get("result")
    assert row["locked_by"] is None
    # 2. 成员事实真的写进了 PostgreSQL（memory_commits 绑定成员 operation）
    async with session_factory() as session:
        commits = await session.execute(
            text(
                "SELECT operation_id, after_version FROM memory_commits "
                "WHERE user_id = :u ORDER BY created_at, operation_id"
            ),
            {"u": USER},
        )
        commit_rows = commits.mappings().all()
    # 两条成员各自写出自己的事实：提交绑定到成员 operation（而非批次）
    assert {row["operation_id"] for row in commit_rows} == {
        first.operation_id,
        second.operation_id,
    }
    assert {int(row["after_version"]) for row in commit_rows} == {1}
    # 3. 成员状态与父批次一致（同一事务回写）
    assert await _operation_status(session_factory, first.operation_id) == "succeeded"
    assert await _operation_status(session_factory, second.operation_id) == "succeeded"
    # 4. Markdown 侧可读：两个主题文档都在，且 current/ 已物化
    assert await memory_service.get_mastery(user_id=USER, topic_key="配方法") is not None
    assert await memory_service.get_mastery(user_id=USER, topic_key="椭圆定义") is not None
    root = Path(settings.memory_storage_root)
    # `current/` 物化副本按 logical_path_for 命名（mastery/<topic>.md）
    materialized = sorted(
        path.relative_to(root).as_posix() for path in await asyncio.to_thread(_mastery_files, root)
    )
    assert any("current/mastery/" in name for name in materialized), materialized
    # 5. 两条成员各自的提交都成功了（同一批两条 mastery 文档，无跳过、无失败）
    assert not any("未直接写入" in warning for warning in row["result"].get("warnings") or [])
    async with session_factory() as session:
        docs = await session.execute(
            text(
                "SELECT memory_id FROM memory_documents "
                "WHERE user_id = :u AND memory_id LIKE 'mastery:%' ORDER BY memory_id"
            ),
            {"u": USER},
        )
    assert [r["memory_id"] for r in docs.mappings().all()] == [
        "mastery:椭圆定义",
        "mastery:配方法",
    ]


@pytest.mark.asyncio
async def test_real_worker_isolates_single_member_failure(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    """I-9：一条成员抛非预算类异常 → 其余成员仍写入、该成员进 batch_failed、批次有终态。"""
    bad = await _persist_evidence(
        session_factory, user_id=USER, thread_id="t-iso-bad", next_run_at=NOW
    )
    good = await _persist_evidence(
        session_factory,
        user_id=USER,
        thread_id="t-iso-good",
        next_run_at=NOW + timedelta(seconds=1),
    )
    fake_conversation_reader.add_message(
        "t-iso-bad",
        SourceItem(source_ref="m1", role="user", content="这条会让模型输出不合法", occurred_at=NOW),
    )
    fake_conversation_reader.add_message(
        "t-iso-good",
        SourceItem(source_ref="m1", role="user", content="我用配方法解出方程", occurred_at=NOW),
    )
    # 第一条成员的抽取抛非预算类异常；第二条照常成功
    fake_llm.extract_queue.append(RuntimeError("poison 成员：模型输出无法解析" + "x" * 400))
    _queue_member(fake_llm, "配方法")
    batch = await _build_batch(
        session_factory, user_id=USER, member_ids=[bad.operation_id, good.operation_id]
    )

    worker = _make_worker(session_factory, runner)
    row = await _claim_and_execute(session_factory, worker, batch.operation_id)

    assert row["status"] == "succeeded"
    result = row["result"] or {}
    warnings = result.get("warnings") or []
    # 失败成员被隔离：稳定 reason + 节点名 + 异常类型都在公开可见的 warnings 里
    # （MemoryOperationResult extra="forbid"，没有自由诊断字段，warnings 是唯一出口）
    isolated = [w for w in warnings if "处理失败已隔离" in w]
    assert len(isolated) == 1, warnings
    assert str(bad.operation_id) in isolated[0]
    assert "member_exception" in isolated[0]
    assert "@extract_candidates" in isolated[0]
    assert "RuntimeError" in isolated[0]
    assert len(isolated[0]) < 400, "异常摘要必须截断（≤200 字符）"
    assert any("1 条成员未直接写入" in w for w in warnings), warnings

    # 后续成员没有被拖垮：正常成员写进了长期记忆（且只有它写了）
    assert await memory_service.get_mastery(user_id=USER, topic_key="配方法") is not None
    async with session_factory() as session:
        docs = await session.execute(
            text(
                "SELECT memory_id FROM memory_documents "
                "WHERE user_id = :u AND memory_id LIKE 'mastery:%'"
            ),
            {"u": USER},
        )
        commits = await session.execute(
            text("SELECT operation_id FROM memory_commits WHERE user_id = :u"), {"u": USER}
        )
    assert [r["memory_id"] for r in docs.mappings().all()] == ["mastery:配方法"]
    assert [r["operation_id"] for r in commits.mappings().all()] == [good.operation_id]
    # 批次有终态；**被隔离的失败成员不能被标记为 succeeded**（review I-9 的重试出口）：
    # 它必须回到证据池（pending_batch + 归属清空），等下一次批量重试；若被置 succeeded，
    # 这条证据就永远不会再被处理。
    assert await _operation_status(session_factory, bad.operation_id) == "pending_batch"
    async with session_factory() as session:
        released = (
            (
                await session.execute(
                    text(
                        "SELECT batch_operation_id FROM memory_operations "
                        "WHERE operation_id = :operation_id"
                    ),
                    {"operation_id": bad.operation_id},
                )
            )
            .mappings()
            .one()
        )
    assert released["batch_operation_id"] is None, "失败成员必须释放回池子才能被重试"
    # 成功的成员仍与父批次一致
    assert await _operation_status(session_factory, good.operation_id) == "succeeded"
    assert await _operation_status(session_factory, good.operation_id) == "succeeded"


# ---------------------------------------------------------------------------
# review-2 新发现 12 / 13 / 14：批次级失败信号、失败计数与"已取消/已释放"措辞
# ---------------------------------------------------------------------------


async def _operation_row(
    session_factory: async_sessionmaker[AsyncSession], operation_id: UUID
) -> dict[str, Any]:
    async with session_factory() as session:
        row = await ops_repo.get_operation(session, operation_id)
    assert row is not None
    return dict(row)


@pytest.mark.asyncio
async def test_all_members_failed_batch_goes_dead_letter(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    """新发现 12：**整批全灭**时批次必须进 dead_letter，而不是 succeeded。

    旧实现只把成员失败记进 ``batch_failed`` 与 warnings，``errors`` 为空 →
    ``normalize_result`` 判 succeeded：operation 状态与实际写入量（0 条）不符。
    """
    first = await _persist_evidence(
        session_factory, user_id=USER, thread_id="t-allfail-1", next_run_at=NOW
    )
    second = await _persist_evidence(
        session_factory,
        user_id=USER,
        thread_id="t-allfail-2",
        next_run_at=NOW + timedelta(seconds=1),
    )
    fake_conversation_reader.add_message(
        "t-allfail-1",
        SourceItem(source_ref="m1", role="user", content="毒药一", occurred_at=NOW),
    )
    fake_conversation_reader.add_message(
        "t-allfail-2",
        SourceItem(source_ref="m1", role="user", content="毒药二", occurred_at=NOW),
    )
    # 两条成员都抛非致命异常 → 都会被成员级隔离捕获
    fake_llm.extract_queue.append(RuntimeError("poison-1"))
    fake_llm.extract_queue.append(RuntimeError("poison-2"))
    batch = await _build_batch(
        session_factory, user_id=USER, member_ids=[first.operation_id, second.operation_id]
    )

    worker = _make_worker(session_factory, runner)
    row = await _claim_and_execute(session_factory, worker, batch.operation_id)

    assert row["status"] == "dead_letter", (
        f"整批零写入必须判 dead_letter（人工审），实际 {row['status']}：{row.get('result')}"
    )
    error = row["public_error"] or {}
    assert error.get("code") == batch_module.BATCH_ALL_MEMBERS_FAILED, error
    assert "2/2" in str(error.get("message")), error
    # 公开可见面仍有信息量：人数 + 首条失败摘要进 public_error.message
    assert "member_exception" in str(error.get("message"))
    # 没有任何事实被写入
    assert await memory_service.get_mastery(user_id=USER, topic_key="配方法") is None
    async with session_factory() as session:
        commits = await session.execute(
            text("SELECT count(*) FROM memory_commits WHERE user_id = :u"), {"u": USER}
        )
        assert int(commits.scalar_one()) == 0

    # 失败成员仍然先被释放回证据池（重试出口不因批次判死而丢），且尝试计数 +1
    for member in (first, second):
        member_row = await _operation_row(session_factory, member.operation_id)
        assert member_row["status"] == "pending_batch"
        assert member_row["batch_operation_id"] is None, "失败成员必须释放回池子"
        assert int(member_row["attempt_count"]) == 1, "释放必须留下可观测的尝试计数"


@pytest.mark.asyncio
async def test_failed_member_escalates_to_dead_letter_at_max_attempts(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    """新发现 13：失败成员达到自己的 max_attempts 后转 dead_letter，不再无限重试。"""
    member = await _persist_evidence(
        session_factory, user_id=USER, thread_id="t-escalate", next_run_at=NOW
    )
    fake_conversation_reader.add_message(
        "t-escalate", SourceItem(source_ref="m1", role="user", content="毒药", occurred_at=NOW)
    )
    fake_llm.extract_queue.append(RuntimeError("poison"))
    # 把该成员推到"下一次失败就到上限"的位置（证据 operation 是 P2 → max_attempts=4）
    async with session_factory() as session:
        async with session.begin():
            updated = await session.execute(
                text(
                    "UPDATE memory_operations SET attempt_count = max_attempts - 1 "
                    "WHERE operation_id = :operation_id RETURNING attempt_count, max_attempts"
                ),
                {"operation_id": member.operation_id},
            )
            before = updated.mappings().one()
    assert int(before["max_attempts"]) == 4, before

    batch = await _build_batch(session_factory, user_id=USER, member_ids=[member.operation_id])
    worker = _make_worker(session_factory, runner)
    row = await _claim_and_execute(session_factory, worker, batch.operation_id)

    # 批次级信号（新发现 12）与成员级终态（新发现 13）同时成立
    assert row["status"] == "dead_letter", row.get("result")
    member_row = await _operation_row(session_factory, member.operation_id)
    assert member_row["status"] == "dead_letter", "达到 max_attempts 必须转人工审核"
    assert int(member_row["attempt_count"]) == int(before["max_attempts"])
    assert member_row["batch_operation_id"] == batch.operation_id, "死信保留归属以便追溯"
    assert (member_row["public_error"] or {}).get("code") == "OPERATION_DEAD_LETTER"
    # 不再出现在证据池里 → 不会被下一批重复领取
    async with session_factory() as session:
        pool = await ops_repo.list_pending_batch_members(
            session, user_id=USER, now=datetime.now(UTC), limit=10
        )
    assert [UUID(str(item["operation_id"])) for item in pool] == []


@pytest.mark.asyncio
async def test_load_batch_members_separates_cancelled_from_missing(
    session_factory: async_sessionmaker[AsyncSession],
    runtime_context: MemoryRuntimeContext,
) -> None:
    """新发现 14：已取消/已释放的成员是**按设计跳过**，不能报成"归属丢失"。"""
    cancelled = await _persist_evidence(
        session_factory, user_id=USER, thread_id="t-declared-cancelled", next_run_at=NOW
    )
    kept = await _persist_evidence(
        session_factory,
        user_id=USER,
        thread_id="t-declared-kept",
        next_run_at=NOW + timedelta(seconds=1),
    )
    ghost_id = uuid4()
    batch_operation_id = uuid4()
    batch = make_operation(
        user_id=USER,
        actor_type="system",
        input_kind="evidence",
        operation_type="summarize_user_memory_batch",
        priority=50,
        payload=SummarizeUserMemoryBatchCommand(
            target_user_id=USER,
            batch_operation_id=batch_operation_id,
            member_operation_ids=[cancelled.operation_id, ghost_id, kept.operation_id],
            max_evidence=50,
        ),
    ).model_copy(update={"operation_id": batch_operation_id})
    await persist_operation(session_factory, batch)
    async with session_factory() as session:
        async with session.begin():
            assigned = await ops_repo.assign_batch_members(
                session,
                batch_operation_id=batch_operation_id,
                member_operation_ids=[cancelled.operation_id, kept.operation_id],
            )
    assert assigned == 2
    # 用户取消其中一条（状态 cancelled，归属仍在）
    async with session_factory() as session:
        async with session.begin():
            await ops_repo.request_cancel(session, operation_id=cancelled.operation_id)

    state = {"operation": batch.model_dump(mode="json")}
    loaded = await batch_module.load_batch_members(state, _runtime(runtime_context))

    assert [m["operation_id"] for m in loaded["batch_members"]] == [str(kept.operation_id)]
    warnings = loaded["batch_warnings"]
    # 已取消的成员：中性措辞，不是"归属丢失"
    assert not any("无归属" in w for w in warnings), warnings
    assert not any(str(cancelled.operation_id) in w for w in warnings), warnings
    skipped = [
        entry
        for entry in loaded["batch_processed"]
        if entry["operation_id"] == str(cancelled.operation_id)
    ]
    assert len(skipped) == 1, loaded["batch_processed"]
    assert skipped[0]["outcome"] == batch_module.MEMBER_OUTCOME_UNAVAILABLE
    assert "已取消/已释放" in skipped[0]["reason"]
    # 真·不存在的那条：仍然要警告，并点明是不一致
    assert any("在 memory_operations 中不存在" in w and "数据不一致" in w for w in warnings), (
        warnings
    )
    assert any(str(ghost_id) not in w for w in warnings)


def _runtime(runtime_context: MemoryRuntimeContext) -> Any:
    """把 fixture 的 runtime context 包成 graph 节点要的 Runtime（只用到 .context）。"""

    class _Runtime:
        context = runtime_context

    return _Runtime()


# ---------------------------------------------------------------------------
# errors-only 成员的终态语义（复审遗留 DEV-072）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_errors_only_member_is_released_and_visible_in_warnings(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    """DEV-072：混合批次里"有 errors、无 mutation、无审核候选"的成员不能被算作完成。

    旧行为：该成员记 ``no_change`` 且**不释放**，批次 succeeded 时
    ``settle_batch_members`` 把它一起置 succeeded —— 等于"没写进去却算完成"，
    而且它再也不会被重试（公开 warnings 里只有一句笼统的"少写了一条"）。
    """
    broken = await _persist_evidence(
        session_factory, user_id=USER, thread_id="t-errsonly-bad", next_run_at=NOW
    )
    good = await _persist_evidence(
        session_factory,
        user_id=USER,
        thread_id="t-errsonly-good",
        next_run_at=NOW + timedelta(seconds=1),
    )
    fake_conversation_reader.add_message(
        "t-errsonly-bad",
        SourceItem(source_ref="m1", role="user", content="这条会预算耗尽", occurred_at=NOW),
    )
    fake_conversation_reader.add_message(
        "t-errsonly-good",
        SourceItem(source_ref="m1", role="user", content="我用配方法解出方程", occurred_at=NOW),
    )
    # 第一条成员的抽取"预算耗尽"：节点把 LLM_BUDGET_EXHAUSTED 写进 state["errors"]，
    # 零候选 → 零 mutation、零审核候选（正是 errors-only 形态）；第二条照常成功
    fake_llm.extract_queue.append(LLMBudgetExceededError("LLM 调用预算耗尽"))
    _queue_member(fake_llm, "配方法")
    batch = await _build_batch(
        session_factory, user_id=USER, member_ids=[broken.operation_id, good.operation_id]
    )

    worker = _make_worker(session_factory, runner)
    row = await _claim_and_execute(session_factory, worker, batch.operation_id)

    # 单成员失败不拖垮整批（§5.8）：有写入 → 批次 succeeded
    assert row["status"] == "succeeded", row.get("result")
    warnings = (row["result"] or {}).get("warnings") or []
    # 未写入计数只算真正没写进去的那条（不是把整批都算进去）
    assert any("本批 1 条成员未直接写入" in item for item in warnings), warnings
    # 逐条可观测：是哪条成员、为什么（errors-only 的错误码进公开 warnings）
    assert any(
        str(broken.operation_id) in item
        and "未写入任何变更" in item
        and "LLM_BUDGET_EXHAUSTED" in item
        for item in warnings
    ), warnings
    assert any("1 条失败成员已释放回证据池" in item for item in warnings), warnings

    # 出错那条必须回到证据池（pending_batch + 归属清空 + attempt_count 计数），
    # **不能**被 settle 成 succeeded —— 否则这条证据永远不会再被处理
    released = await _operation_row(session_factory, broken.operation_id)
    assert released["status"] == "pending_batch", released
    assert released["batch_operation_id"] is None, "errors-only 成员必须释放回池子"
    assert int(released["attempt_count"]) == 1, "释放必须留下可观测的尝试计数"
    # 下一批会重新认领它
    async with session_factory() as session:
        pool = await ops_repo.list_pending_batch_members(
            session, user_id=USER, now=datetime.now(UTC), limit=10
        )
    assert [UUID(str(item["operation_id"])) for item in pool] == [broken.operation_id]

    # 正常那条：事实真的写进长期记忆，成员与父批次一致
    assert await memory_service.get_mastery(user_id=USER, topic_key="配方法") is not None
    assert await _operation_status(session_factory, good.operation_id) == "succeeded"


@pytest.mark.asyncio
async def test_errors_only_member_escalates_to_dead_letter_and_batch_stays_dead_letter(
    runner: LocalLangGraphRunner,
    session_factory: async_sessionmaker[AsyncSession],
    fake_llm: FakeMemoryLLMClient,
    fake_conversation_reader: FakeConversationReader,
) -> None:
    """DEV-072 + 新发现 13 的衔接：errors-only 成员达 ``max_attempts`` 转死信；

    同一条路径下"整批全灭"仍然落 ``dead_letter``（既有行为回归，只是失败原因换成 errors-only）。
    """
    member = await _persist_evidence(
        session_factory, user_id=USER, thread_id="t-errsonly-escalate", next_run_at=NOW
    )
    fake_conversation_reader.add_message(
        "t-errsonly-escalate",
        SourceItem(source_ref="m1", role="user", content="预算耗尽", occurred_at=NOW),
    )
    fake_llm.extract_queue.append(LLMBudgetExceededError("LLM 调用预算耗尽"))
    # 把该成员推到"下一次失败就到上限"的位置（证据 operation 是 P2 → max_attempts=4）
    async with session_factory() as session:
        async with session.begin():
            updated = await session.execute(
                text(
                    "UPDATE memory_operations SET attempt_count = max_attempts - 1 "
                    "WHERE operation_id = :operation_id RETURNING attempt_count, max_attempts"
                ),
                {"operation_id": member.operation_id},
            )
            before = updated.mappings().one()
    assert int(before["max_attempts"]) == 4, before

    batch = await _build_batch(session_factory, user_id=USER, member_ids=[member.operation_id])
    worker = _make_worker(session_factory, runner)
    row = await _claim_and_execute(session_factory, worker, batch.operation_id)

    # 整批全灭（0 mutation / 0 审核候选）仍是 dead_letter，且 code 能区分"全灭"
    assert row["status"] == "dead_letter", row.get("result")
    error = row["public_error"] or {}
    assert error.get("code") == batch_module.BATCH_ALL_MEMBERS_FAILED, error
    message = str(error.get("message"))
    assert "1/1" in message, message
    # errors-only 的摘要形态：稳定 reason + 错误码（异常摘要在这种失败里不存在）
    assert batch_module.MEMBER_ERRORS_REASON in message, message
    assert "LLM_BUDGET_EXHAUSTED" in message, message

    # 成员级终态由释放路径决定（与批次级 dead_letter 互不覆盖）
    member_row = await _operation_row(session_factory, member.operation_id)
    assert member_row["status"] == "dead_letter", "达到 max_attempts 必须转人工审核"
    assert int(member_row["attempt_count"]) == int(before["max_attempts"])
    assert member_row["batch_operation_id"] == batch.operation_id, "死信保留归属以便追溯"
    assert (member_row["public_error"] or {}).get("code") == "OPERATION_DEAD_LETTER"
