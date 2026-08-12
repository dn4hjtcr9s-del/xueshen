"""失败恢复测试（§23.4 / 评审 #5/#6/#11）：mutation replay 幂等语义。

模拟 Worker 在终态落库前崩溃、以相同 mutation/operation 重放的场景：
- #5  commit_plans：重放不得渲染/写新版本文件、不得重写 current/，返回原 commit；
- #6  forget/restore：重放优先于状态检查，直接返回原结果而非 tombstone/版本冲突；
- #11 图谱用户命令：Overlay 事务已提交的重放凭 graph_state_audit 重建首次结果。
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import CommitMutationPlan, LearnerPatch, MasteryPatch
from backend.memory.contracts.errors import InvalidPayloadError
from backend.memory.services.graph_state_service import KnowledgeGraphStateService
from backend.memory.services.memory_service import MemoryService
from backend.settings import Settings

USER = UUID("00000000-0000-4000-8000-0000000000c1")
GRAPH_USER = UUID("00000000-0000-4000-8000-0000000000c2")
HEX64 = "ab" * 32


async def _insert_operation(session: AsyncSession, operation_id: UUID, user_id: UUID) -> None:
    await session.execute(
        text(
            "INSERT INTO memory_operations ("
            "operation_id, user_id, actor_type, input_kind, operation_type,"
            "idempotency_key, idempotency_payload_hash, priority, status,"
            "payload, trace_id, graph_thread_id, occurred_at, max_attempts"
            ") VALUES ("
            ":operation_id, :user_id, 'user', 'command', 'correct_memory',"
            ":idem, :idem_hash, 10, 'running',"
            "'{}'::jsonb, :trace_id, 'graph-test', now(), 3)"
        ),
        {
            "operation_id": operation_id,
            "user_id": user_id,
            "idem": f"idem-{operation_id}",
            "idem_hash": "0" * 64,
            "trace_id": uuid4().hex + uuid4().hex,
        },
    )


def _learner_plan(mutation_id: UUID | None = None, *, action: str = "create") -> CommitMutationPlan:
    return CommitMutationPlan(
        mutation_id=mutation_id or uuid4(),
        memory_id="learner",
        target_memory_type="learner",
        action=action,  # type: ignore[arg-type]
        learner_patch=LearnerPatch(goals_to_add=["期中考试达到 90 分"]),
    )


def _mastery_plan(mutation_id: UUID | None = None) -> CommitMutationPlan:
    return CommitMutationPlan(
        mutation_id=mutation_id or uuid4(),
        memory_id="mastery:yici-hanshu",
        target_memory_type="mastery",
        topic_title="一次函数",
        action="create",
        mastery_patch=MasteryPatch(overview="基本掌握", understood_to_add=["定义"]),
    )


async def _commit(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    plans: list[CommitMutationPlan],
    operation_id: UUID | None = None,
    *,
    insert_operation: bool = True,
):
    op_id = operation_id or uuid4()
    if insert_operation:
        async with session_factory() as session:
            async with session.begin():
                await _insert_operation(session, op_id, USER)
    return await memory_service.commit_plans(
        operation_id=op_id, user_id=USER, actor_type="user", plans=plans
    )


def _snapshot_files(root: Path) -> dict[str, bytes]:
    if not root.exists():
        return {}
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# 评审 #5：commit_plans 重放在渲染/文件副作用之前判定
# ---------------------------------------------------------------------------


async def test_commit_replay_after_crash_skips_render_and_file_writes(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """首次提交成功、终态落库前崩溃 → 同 mutation 重放：无新文件、不改 current/。"""
    plans = [_learner_plan(), _mastery_plan()]
    op_id = uuid4()
    outcome1 = await _commit(memory_service, session_factory, plans, op_id)
    assert len(outcome1.mutations) == 2

    root = Path(settings.memory_storage_root)
    before = _snapshot_files(root)
    assert before, "首次提交后应存在版本与 current 文件"

    # Worker 重放：同一 operation、同一 plans（mutation_id 相同）
    outcome2 = await _commit(memory_service, session_factory, plans, op_id, insert_operation=False)
    assert outcome2.replayed is True
    for first, second in zip(outcome1.mutations, outcome2.mutations, strict=True):
        assert second.mutation_id == first.mutation_id
        assert second.action == first.action
        assert second.before_version == first.before_version
        assert second.after_version == first.after_version

    after = _snapshot_files(root)
    assert after == before, "重放不得产生新版本文件，也不得重写 current/"

    async with session_factory() as session:
        commits = await session.execute(text("SELECT COUNT(*) FROM memory_commits"))
        assert int(commits.scalar_one()) == 2, "重放不得产生新 commit 行"


async def test_commit_partial_replay_only_renders_new_mutations(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """部分重放：已提交的 plan 跳过文件写入，新 plan 正常提交，返回顺序稳定。"""
    learner_plan = _learner_plan()
    await _commit(memory_service, session_factory, [learner_plan])

    root = Path(settings.memory_storage_root)
    learner_files = {
        path: content for path, content in _snapshot_files(root).items() if "learner" in path
    }

    mastery_plan = _mastery_plan()
    outcome = await _commit(memory_service, session_factory, [learner_plan, mastery_plan])
    assert outcome.replayed is True
    assert [m.action for m in outcome.mutations] == ["create", "create"]
    # 重放的 learner 返回原版本号，新 mastery 为版本 1
    assert outcome.mutations[0].after_version == 1
    assert outcome.mutations[1].after_version == 1

    after = _snapshot_files(root)
    for path, content in learner_files.items():
        assert after.get(path) == content, f"重放不得改动 learner 文件: {path}"
    assert any("yici-hanshu" in path for path in after), "新 mastery 必须正常落盘"


async def test_commit_replay_rejects_inconsistent_mutation(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """mutation_id 撞车但 action/memory/user 不一致：按非法 payload 拒绝。"""
    plan = _learner_plan()
    await _commit(memory_service, session_factory, [plan])
    # 同一 mutation_id 但 memory_id 不一致（model_copy 绕过契约校验，模拟脏数据重放）
    tampered = plan.model_copy(update={"memory_id": "mastery:yici-hanshu"})
    with pytest.raises(InvalidPayloadError, match="不一致"):
        await _commit(memory_service, session_factory, [tampered])


# ---------------------------------------------------------------------------
# 评审 #6：forget/restore 重放优先于状态检查
# ---------------------------------------------------------------------------


async def _create_learner(
    memory_service: MemoryService, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _commit(memory_service, session_factory, [_learner_plan()])


async def test_forget_replay_after_crash_returns_original_result(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """forget 首次成功后崩溃重放：返回原 MutationResult 而非 MemoryDeletedError。"""
    await _create_learner(memory_service, session_factory)
    op_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, op_id, USER)
    mutation_id = uuid4()
    first = await memory_service.forget(
        operation_id=op_id,
        user_id=USER,
        actor_type="user",
        mutation_id=mutation_id,
        memory_id="learner",
        expected_version=1,
        reason="用户要求",
    )
    assert first.action == "forget" and first.before_version == 1

    replay = await memory_service.forget(
        operation_id=op_id,
        user_id=USER,
        actor_type="user",
        mutation_id=mutation_id,
        memory_id="learner",
        expected_version=1,
        reason="用户要求",
    )
    assert replay.action == "forget"
    assert replay.before_version == first.before_version
    assert replay.after_version is None

    async with session_factory() as session:
        commits = await session.execute(
            text("SELECT COUNT(*) FROM memory_commits WHERE action = 'forget'")
        )
        assert int(commits.scalar_one()) == 1, "重放不得产生第二条 forget commit"


async def test_restore_replay_after_crash_returns_original_result(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """restore 首次成功后崩溃重放：返回原 MutationResult 而非"未处于删除状态"冲突。"""
    await _create_learner(memory_service, session_factory)
    forget_op = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, forget_op, USER)
    await memory_service.forget(
        operation_id=forget_op,
        user_id=USER,
        actor_type="user",
        mutation_id=uuid4(),
        memory_id="learner",
        expected_version=1,
        reason=None,
    )

    restore_op = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, restore_op, USER)
    mutation_id = uuid4()
    first = await memory_service.restore(
        operation_id=restore_op,
        user_id=USER,
        actor_type="user",
        mutation_id=mutation_id,
        memory_id="learner",
        deleted_version=1,
    )
    assert first.action == "restore" and first.after_version == 2

    replay = await memory_service.restore(
        operation_id=restore_op,
        user_id=USER,
        actor_type="user",
        mutation_id=mutation_id,
        memory_id="learner",
        deleted_version=1,
    )
    assert replay.action == "restore"
    assert replay.after_version == first.after_version

    async with session_factory() as session:
        commits = await session.execute(
            text("SELECT COUNT(*) FROM memory_commits WHERE action = 'restore'")
        )
        assert int(commits.scalar_one()) == 1


async def test_forget_replay_rejects_cross_action_mutation(
    memory_service: MemoryService,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """拿 restore 的 mutation_id 重放 forget：一致性校验拒绝。"""
    await _create_learner(memory_service, session_factory)
    op_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, op_id, USER)
    restore_mutation = uuid4()
    await memory_service.forget(
        operation_id=op_id,
        user_id=USER,
        actor_type="user",
        mutation_id=uuid4(),
        memory_id="learner",
        expected_version=1,
        reason=None,
    )
    restore_op = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, restore_op, USER)
    await memory_service.restore(
        operation_id=restore_op,
        user_id=USER,
        actor_type="user",
        mutation_id=restore_mutation,
        memory_id="learner",
        deleted_version=1,
    )
    with pytest.raises(InvalidPayloadError, match="不一致"):
        await memory_service.forget(
            operation_id=op_id,
            user_id=USER,
            actor_type="user",
            mutation_id=restore_mutation,
            memory_id="learner",
            expected_version=2,
            reason=None,
        )


# ---------------------------------------------------------------------------
# 评审 #11：图谱用户 mutation 的 crash-window replay
# ---------------------------------------------------------------------------


async def _seed_graph_node(session_factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO knowledge_graph_nodes (node_id, title, source_file, "
                    "source_checksum) VALUES ('n9001', '测试节点', 'test.md', :ck) "
                    "ON CONFLICT (node_id) DO NOTHING"
                ),
                {"ck": HEX64},
            )


async def test_graph_user_command_replay_returns_original_change(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Overlay 事务已提交、operation 终态未落库 → 重放凭 audit 重建首次结果。

    修复前：重放对已推进的 Overlay 做版本校验，抛 GraphStateVersionRequiredError；
    修复后：直接返回首次的 graph_state_changes，不推进版本、不写新审计。
    """
    await _seed_graph_node(session_factory)
    service = KnowledgeGraphStateService(settings=settings, session_factory=session_factory)
    op_id = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, op_id, GRAPH_USER)

    first = await service.apply_user_command(
        operation_id=op_id,
        user_id=GRAPH_USER,
        actor_type="user",
        node_id="n9001",
        action="mark_familiar",
        expected_version=None,
    )
    assert first.before_status is None
    assert first.after_status == "proficient"
    assert first.after_version == 1

    replay = await service.apply_user_command(
        operation_id=op_id,
        user_id=GRAPH_USER,
        actor_type="user",
        node_id="n9001",
        action="mark_familiar",
        expected_version=None,
    )
    assert replay.before_status == first.before_status
    assert replay.after_status == first.after_status
    assert replay.before_version == first.before_version
    assert replay.after_version == first.after_version

    async with session_factory() as session:
        audit_row = (
            await session.execute(
                text("SELECT created_at FROM graph_state_audit WHERE operation_id = :op"),
                {"op": op_id},
            )
        ).one()
        # 重放的 changed_at 必须来自首次提交的审计记录（而非重放当下的时间）
        assert replay.changed_at == audit_row[0]
        audits = await session.execute(
            text("SELECT COUNT(*) FROM graph_state_audit WHERE operation_id = :op"),
            {"op": op_id},
        )
        assert int(audits.scalar_one()) == 1, "重放不得写第二条审计"
        overlay = await session.execute(
            text("SELECT version FROM graph_user_states WHERE user_id = :u AND node_id = 'n9001'"),
            {"u": GRAPH_USER},
        )
        assert int(overlay.scalar_one()) == 1, "重放不得推进 Overlay 版本"


async def test_graph_user_command_replay_rejects_inconsistent_audit(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """operation 的图谱审计与当前命令 user/node 不一致：按非法 payload 拒绝。"""
    await _seed_graph_node(session_factory)
    service = KnowledgeGraphStateService(settings=settings, session_factory=session_factory)
    op_id = uuid4()
    other_user = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await _insert_operation(session, op_id, other_user)
            await session.execute(
                text(
                    "INSERT INTO graph_state_audit ("
                    "audit_id, operation_id, user_id, node_id, before_status, "
                    "after_status, before_version, after_version, actor_type"
                    ") VALUES (:aid, :op, :u, 'n9001', NULL, 'proficient', NULL, 1, 'user')"
                ),
                {"aid": uuid4(), "op": op_id, "u": other_user},
            )
    with pytest.raises(InvalidPayloadError, match="不一致"):
        await service.apply_user_command(
            operation_id=op_id,
            user_id=GRAPH_USER,
            actor_type="user",
            node_id="n9001",
            action="clear",
            expected_version=1,
        )
