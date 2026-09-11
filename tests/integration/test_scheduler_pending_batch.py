"""证据池 nightly 入批的集成测试（memory-rebuild §2.6 / §5.8 Phase 6 验收）。

真实 PostgreSQL（``memory_test``）+ 真实 ``memory_operations`` / ``memory_maintenance_runs``
行，验证只有落库才能证明的部分：

- ``next_run_at`` 门控准确：未到点的证据不被扫描；
- 跨用户不混批；
- 单批不超过 ``batch_max_evidence``，跑不完靠 run.cursor 续跑，**不丢不重**；
- 已归属（在途或已完成）的证据不会被第二个批次重复入批。

批量图本身不在本文件覆盖（属 graph 侧测试），这里只模拟"批次 operation 跑完"这一步：
把批次 operation 置 succeeded 并按既有语义回写成员状态。
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.batch import BATCH_IDEMPOTENCY_KEY_TEMPLATE, BatchCursor
from backend.memory.contracts.common import PRIORITY_P2, idempotency_payload_hash
from backend.memory.contracts.evidence import ConversationEvidence
from backend.memory.persistence import maintenance as maintenance_repo
from backend.memory.persistence import operations as ops_repo
from backend.memory.worker.scheduler import Scheduler, SchedulerConfig
from tests.integration.graph_helpers import make_operation

#: 本地（Asia/Shanghai）2026-09-11 00:05 —— 跨过 0 点，日期取 0 点批量任务的当天。
NOW = datetime(2026, 9, 10, 16, 5, tzinfo=UTC)
LOCAL_DATE = "2026-09-11"


def _scheduler(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    max_evidence: int = 50,
    max_users: int = 50,
) -> Scheduler:
    return Scheduler(
        session_factory=session_factory,
        config=SchedulerConfig(
            evidence_batch_enabled=True,
            batch_max_evidence=max_evidence,
            batch_max_users_per_run=max_users,
            summary_daily_time=time(0, 0),
        ),
        clock=lambda: NOW,
    )


async def _seed_evidence(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    user_id: UUID,
    next_run_at: datetime,
) -> UUID:
    """插入一条 ``pending_batch`` 证据行（模拟 §2.6 状态机第 1 步的提交结果）。"""
    operation = make_operation(
        user_id=user_id,
        actor_type="conversation_agent",
        input_kind="evidence",
        operation_type="conversation_evidence",
        priority=PRIORITY_P2,
        payload=ConversationEvidence(
            thread_id=f"thread-{uuid4().hex[:8]}",
            message_ids=[f"message-{uuid4().hex[:8]}"],
            trigger="turn_boundary",
        ),
    )
    async with session_factory() as session:
        async with session.begin():
            inserted = await ops_repo.insert_operation(
                session,
                operation,
                idempotency_payload_hash=idempotency_payload_hash(
                    operation.payload.model_dump(mode="json")
                ),
                status=ops_repo.PENDING_BATCH_STATUS,
                next_run_at=next_run_at,
            )
            assert inserted
    return operation.operation_id


async def _operation_row(
    session_factory: async_sessionmaker[AsyncSession], operation_id: UUID
) -> dict[str, Any]:
    async with session_factory() as session:
        row = await ops_repo.get_operation(session, operation_id)
    assert row is not None
    return row


async def _batch_rows(
    session_factory: async_sessionmaker[AsyncSession], user_id: UUID
) -> list[dict[str, Any]]:
    """该用户全部批量 operation（按创建序）。"""
    async with session_factory() as session:
        result = await session.execute(
            text(
                """
                SELECT * FROM memory_operations
                WHERE user_id = :user_id
                  AND operation_type = 'summarize_user_memory_batch'
                ORDER BY created_at ASC, operation_id ASC
                """
            ),
            {"user_id": user_id},
        )
        return [dict(row) for row in result.mappings().all()]


async def _run_row(
    session_factory: async_sessionmaker[AsyncSession], *, user_id: UUID
) -> dict[str, Any]:
    key = BATCH_IDEMPOTENCY_KEY_TEMPLATE.format(user_id=user_id, date=LOCAL_DATE)
    async with session_factory() as session:
        row = await maintenance_repo.get_run_by_key(session, idempotency_key=key)
    assert row is not None, f"未找到 run {key}"
    return dict(row)


def _member_ids(batch_row: dict[str, Any]) -> list[str]:
    return [str(item) for item in batch_row["payload"]["member_operation_ids"]]


async def _finish_batch(
    session_factory: async_sessionmaker[AsyncSession], batch_operation_id: UUID
) -> None:
    """模拟批量图把批次跑到成功：operation 终态 + 同一事务回写成员（§2.6 第 3 步）。"""
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE memory_operations SET status = 'succeeded', completed_at = now(), "
                    "updated_at = now() WHERE operation_id = :operation_id"
                ),
                {"operation_id": batch_operation_id},
            )
            await ops_repo.settle_batch_members(
                session, batch_operation_id=batch_operation_id, status="succeeded"
            )


# ---------------------------------------------------------------------------
# next_run_at 门控
# ---------------------------------------------------------------------------


async def test_gate_scans_only_due_evidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """门控到点才入批：未到点的行不被扫描，也不产生第二个批次。"""
    user_id = uuid4()
    due_id = await _seed_evidence(
        session_factory, user_id=user_id, next_run_at=NOW - timedelta(hours=1)
    )
    not_due_id = await _seed_evidence(
        session_factory, user_id=user_id, next_run_at=NOW + timedelta(hours=3)
    )
    scheduler = _scheduler(session_factory)

    assert await scheduler.run_task("summarize_pending_evidence", NOW) is True

    batches = await _batch_rows(session_factory, user_id)
    assert len(batches) == 1
    batch = batches[0]
    assert _member_ids(batch) == [str(due_id)], "未到点的证据不得入批"
    assert batch["status"] == "queued", "批次自身是可被 Worker 认领的 queued 任务"
    assert batch["input_kind"] == "evidence"
    assert batch["actor_type"] == "system"
    assert batch["priority"] == PRIORITY_P2
    assert batch["next_run_at"] is not None, "批次 operation 走 DDL 默认（now()）即可被认领"

    # run 幂等键 = summarize:{user_id}:{date}；cursor 落在唯一成员上
    run = await _run_row(session_factory, user_id=user_id)
    assert run["maintenance_type"] == "summarize_pending_evidence"
    assert run["status"] == "running"
    assert run["operation_id"] == batch["operation_id"]
    assert BatchCursor.decode(run["cursor"]).operation_id == due_id
    assert batch["idempotency_key"] == f"{run['idempotency_key']}:initial"

    # 第二条证据未到点：再次调度既不入批也不新建批次
    assert await scheduler.run_task("summarize_pending_evidence", NOW) is False
    assert len(await _batch_rows(session_factory, user_id)) == 1
    assert (await _operation_row(session_factory, not_due_id))["batch_operation_id"] is None


# ---------------------------------------------------------------------------
# 跨用户隔离
# ---------------------------------------------------------------------------


async def test_cross_user_batches_do_not_mix(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """两个用户各 1 条 → 两个批次，成员不串。"""
    first_user, second_user = uuid4(), uuid4()
    first_evidence = await _seed_evidence(
        session_factory, user_id=first_user, next_run_at=NOW - timedelta(hours=2)
    )
    second_evidence = await _seed_evidence(
        session_factory, user_id=second_user, next_run_at=NOW - timedelta(hours=1)
    )

    assert await _scheduler(session_factory).run_task("summarize_pending_evidence", NOW) is True

    first_batch = (await _batch_rows(session_factory, first_user))[0]
    second_batch = (await _batch_rows(session_factory, second_user))[0]
    assert first_batch["operation_id"] != second_batch["operation_id"]
    assert first_batch["payload"]["target_user_id"] == str(first_user)
    assert second_batch["payload"]["target_user_id"] == str(second_user)
    assert _member_ids(first_batch) == [str(first_evidence)]
    assert _member_ids(second_batch) == [str(second_evidence)]
    assert (await _operation_row(session_factory, first_evidence))[
        "batch_operation_id"
    ] == first_batch["operation_id"]
    assert (await _operation_row(session_factory, second_evidence))[
        "batch_operation_id"
    ] == second_batch["operation_id"]
    # 各自一个 run
    assert (await _run_row(session_factory, user_id=first_user))["status"] == "running"
    assert (await _run_row(session_factory, user_id=second_user))["status"] == "running"


# ---------------------------------------------------------------------------
# 有界批量 + cursor 续跑
# ---------------------------------------------------------------------------


async def test_bounded_batch_resumes_by_cursor_without_loss_or_duplication(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """上限 2、共 3 条：第一批 2 条，续跑拿剩下 1 条，不丢不重。"""
    user_id = uuid4()
    rows = [
        await _seed_evidence(
            session_factory,
            user_id=user_id,
            next_run_at=NOW - timedelta(hours=offset),
        )
        for offset in (3, 2, 1)
    ]
    scheduler = _scheduler(session_factory, max_evidence=2)

    assert await scheduler.run_task("summarize_pending_evidence", NOW) is True
    first = (await _batch_rows(session_factory, user_id))[0]
    assert _member_ids(first) == [str(rows[0]), str(rows[1])]
    third = await _operation_row(session_factory, rows[2])
    assert third["batch_operation_id"] is None, "超出单批上限的第 3 条本批不入批"
    run = await _run_row(session_factory, user_id=user_id)
    assert BatchCursor.decode(run["cursor"]).operation_id == rows[1], "cursor 推到最后一条成员"

    # 第一批跑完 → 续排下一批（cursor 之后的行）
    await _finish_batch(session_factory, first["operation_id"])
    assert await scheduler.run_task("summarize_pending_evidence", NOW) is True
    second = (await _batch_rows(session_factory, user_id))[1]
    assert _member_ids(second) == [str(rows[2])], "续跑只取 cursor 之后的未归属证据"
    assert (await _operation_row(session_factory, rows[2]))["batch_operation_id"] == second[
        "operation_id"
    ]
    run = await _run_row(session_factory, user_id=user_id)
    assert BatchCursor.decode(run["cursor"]).operation_id == rows[2]

    # 第二批也跑完 → 已无未归属的到期证据，不再新建批次
    await _finish_batch(session_factory, second["operation_id"])
    assert await scheduler.run_task("summarize_pending_evidence", NOW) is False
    batches = await _batch_rows(session_factory, user_id)
    assert len(batches) == 2, "不得重复建批次"
    run = await _run_row(session_factory, user_id=user_id)
    # 该用户已没有未归属的到期证据：run 由同一 tick 的收尾 sweep 关成 succeeded
    # （OPEN-011 用户 2026-09-12 裁决 A；此前会停在 running）。
    # 关键验收仍是不重不漏：run 关闭后也不会再领走任何已归属证据。
    assert run["status"] == "succeeded"

    # 不丢不重：三条证据各自恰好归属一个批次，且互不重叠
    owners: list[UUID] = []
    for evidence_id in rows:
        owner = (await _operation_row(session_factory, evidence_id))["batch_operation_id"]
        assert owner is not None, "证据不得丢失（必须归属某个批次）"
        owners.append(owner)
    assert owners == [first["operation_id"], first["operation_id"], second["operation_id"]]
    assert len(set(owners)) == 2
    for evidence_id in rows:
        member = await _operation_row(session_factory, evidence_id)
        assert member["status"] == "succeeded", "批次成功后成员同事务转 succeeded"


# ---------------------------------------------------------------------------
# 已归属证据不重复入批
# ---------------------------------------------------------------------------


async def test_second_tick_does_not_rebatch_owned_evidence(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """批次在途时再次调度只等待（waiting），不重复入批；成员归属与 cursor 不变。"""
    user_id = uuid4()
    # 上限 2、共 3 条：第 3 条让该用户在本批在途时仍出现在扫描结果里（走到 waiting 分支）
    evidence_ids = [
        await _seed_evidence(
            session_factory, user_id=user_id, next_run_at=NOW - timedelta(hours=offset)
        )
        for offset in (3, 2, 1)
    ]
    scheduler = _scheduler(session_factory, max_evidence=2)

    assert await scheduler.run_task("summarize_pending_evidence", NOW) is True
    first = (await _batch_rows(session_factory, user_id))[0]
    assert _member_ids(first) == [str(evidence_ids[0]), str(evidence_ids[1])]
    run_after_first = await _run_row(session_factory, user_id=user_id)

    # 在途：第二次 tick 返回"待续"，但不新建批次、不动成员归属与 cursor
    assert await scheduler.run_task("summarize_pending_evidence", NOW) is True
    batches = await _batch_rows(session_factory, user_id)
    assert len(batches) == 1, "在途批次未终结，不得为同一 run 建第二个批次"
    assert _member_ids(batches[0]) == [str(evidence_ids[0]), str(evidence_ids[1])]
    for evidence_id in evidence_ids[:2]:
        assert (await _operation_row(session_factory, evidence_id))["batch_operation_id"] == first[
            "operation_id"
        ]
    # 超出上限的那条仍未被任何批次领走（等第一批跑完后续排）
    assert (await _operation_row(session_factory, evidence_ids[2]))["batch_operation_id"] is None
    run_after_second = await _run_row(session_factory, user_id=user_id)
    assert run_after_second["operation_id"] == first["operation_id"]
    assert run_after_second["cursor"] == run_after_first["cursor"]
    assert run_after_second["status"] == "running"


# ---------------------------------------------------------------------------
# run 收尾 sweep（OPEN-011，用户 2026-09-12 裁决 A）
# ---------------------------------------------------------------------------


async def test_sweep_closes_run_when_no_evidence_remains(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """证据全部消费完后，下一次 tick 把当天 run 收尾为 succeeded（不留 running 残留）。"""
    user_id = uuid4()
    await _seed_evidence(session_factory, user_id=user_id, next_run_at=NOW - timedelta(hours=1))
    scheduler = _scheduler(session_factory)

    # 第一轮：入批 → run 被置 running（cursor 待续）
    await scheduler.run_task("summarize_pending_evidence", NOW)
    batches = await _batch_rows(session_factory, user_id)
    assert len(batches) == 1
    run = await _run_row(session_factory, user_id=user_id)
    assert run["status"] == "running"

    # 模拟批量图跑完：operation 终态 + 成员 succeeded
    await _finish_batch(session_factory, UUID(str(batches[0]["operation_id"])))

    # 第二轮：无待入批证据 → sweep 收尾
    assert await scheduler.run_task("summarize_pending_evidence", NOW) is False
    run = await _run_row(session_factory, user_id=user_id)
    assert run["status"] == "succeeded"
    assert run["completed_at"] is not None
    assert run["result"]["reason"] == "swept_no_pending_evidence"
    # 不该产生第二个批次
    assert len(await _batch_rows(session_factory, user_id)) == 1


async def test_sweep_keeps_run_open_while_batch_is_in_flight(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """批次在途时绝不收尾：否则会把正在跑的批次标成成功、断掉该用户的续跑。"""
    user_id = uuid4()
    await _seed_evidence(session_factory, user_id=user_id, next_run_at=NOW - timedelta(hours=1))
    scheduler = _scheduler(session_factory)

    await scheduler.run_task("summarize_pending_evidence", NOW)
    batches = await _batch_rows(session_factory, user_id)
    # 批次仍是 queued（未终态）→ sweep 必须跳过
    assert batches[0]["status"] == "queued"

    await scheduler.run_task("summarize_pending_evidence", NOW)
    run = await _run_row(session_factory, user_id=user_id)
    assert run["status"] == "running"


async def test_sweep_keeps_run_open_when_more_evidence_is_waiting(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """还有未归属的证据时不能收尾：收尾会让剩余证据的续跑断掉。"""
    user_id = uuid4()
    await _seed_evidence(session_factory, user_id=user_id, next_run_at=NOW - timedelta(hours=1))
    await _seed_evidence(session_factory, user_id=user_id, next_run_at=NOW - timedelta(hours=1))
    scheduler = _scheduler(session_factory, max_evidence=1)

    await scheduler.run_task("summarize_pending_evidence", NOW)
    batches = await _batch_rows(session_factory, user_id)
    assert len(batches) == 1 and len(_member_ids(batches[0])) == 1
    await _finish_batch(session_factory, UUID(str(batches[0]["operation_id"])))

    # 第二条证据还没入批 → 不收尾，并在同一 tick 由正常路径排下一批
    await scheduler.run_task("summarize_pending_evidence", NOW)
    run = await _run_row(session_factory, user_id=user_id)
    assert run["status"] == "running"
    assert len(await _batch_rows(session_factory, user_id)) == 2


async def test_sweep_ignores_other_days_runs(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """只收尾当天的 run：昨天遗留的 run 不由今天的 tick 关掉。"""
    user_id = uuid4()
    yesterday_key = BATCH_IDEMPOTENCY_KEY_TEMPLATE.format(user_id=user_id, date="2026-09-10")
    async with session_factory() as session:
        async with session.begin():
            await maintenance_repo.create_or_reuse_run(
                session,
                run_id=uuid4(),
                maintenance_type="summarize_pending_evidence",
                idempotency_key=yesterday_key,
            )
    scheduler = _scheduler(session_factory)
    await scheduler.run_task("summarize_pending_evidence", NOW)

    async with session_factory() as session:
        row = await maintenance_repo.get_run_by_key(session, idempotency_key=yesterday_key)
    assert row is not None
    # 未进过图谱的 run 是 queued；无论 queued 还是 running，今天的 sweep 都不该动它
    assert row["status"] == "queued"
    assert row["completed_at"] is None, "非当天的 run 不得被今天的 sweep 收尾"
