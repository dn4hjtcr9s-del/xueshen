"""migrate_markdown_schema_v2 迁移集成测试（memory-rebuild §5.6 Phase 4 / OPEN-007）。

补上 Phase 4 唯一未闭合的验收项。用**真实 PostgreSQL + 真实文件存储**验证
§5.6「迁移验收」中必须落盘才能证明的部分：

- 同一用户迁移两次：第二次无新版本、无 checksum 抖动、无重复副作用；
- 历史 ``versions/`` 文件**字节级不变**，current 只通过 immutable write 产生新 checksum；
- 坏文档不阻塞同批其他用户；
- 有界 batch + cursor 续跑（跑不完返回 continue + next_cursor）；
- dry_run 不产生任何写入。

测的是"文件系统上到底动没动过"，这正是单元测试覆盖不到的那一层。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import MaintenanceCommand
from backend.memory.contracts.common import SYSTEM_MAINTENANCE_USER_ID
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.graph.state import MemoryRuntimeContext
from backend.memory.persistence import documents as docs_repo
from backend.memory.persistence import maintenance as maintenance_repo
from backend.memory.storage.base import logical_path_for, sha256_hex
from backend.memory.storage.local_markdown import LocalMarkdownStore
from backend.memory.storage.markdown_schema import (
    SCHEMA_VERSION_V1,
    SCHEMA_VERSION_V2,
    LearnerDocument,
    MasteryDocument,
    parse_learner,
    parse_mastery,
    render_learner,
    render_mastery,
)
from tests.integration.graph_helpers import make_operation, persist_operation

NOW = datetime(2026, 9, 10, 4, 15, tzinfo=UTC)


def _v1_learner(user_id: UUID) -> bytes:
    return render_learner(
        LearnerDocument(
            user_id=user_id,
            version=1,
            updated_at=NOW,
            schema_version=SCHEMA_VERSION_V1,
            goals=["期中考试 90 分"],
            preferences=["喜欢图形化讲解"],
            plans=["每天一道极限题"],
        )
    ).encode()


def _v1_mastery(user_id: UUID, topic_key: str) -> bytes:
    return render_mastery(
        MasteryDocument(
            user_id=user_id,
            topic_key=topic_key,
            topic_title=topic_key,
            version=1,
            updated_at=NOW,
            schema_version=SCHEMA_VERSION_V1,
            overview="与 [[抛物线]] 的焦点性质混淆。",
            understood=["逐点收敛定义"],
        )
    ).encode()


def _v2_mastery(user_id: UUID, topic_key: str) -> bytes:
    """已经是 v2 的文档（模拟 frontmatter_patch 原生升版 / 早期迁移跑过）。"""
    return render_mastery(
        MasteryDocument(
            user_id=user_id,
            topic_key=topic_key,
            topic_title=topic_key,
            version=1,
            updated_at=NOW,
            schema_version=SCHEMA_VERSION_V2,
            name=topic_key,
            description="圆锥曲线之一",
            aliases=["ellipse"],
            keywords=["焦点", "准线"],
            links=["抛物线"],
            overview="与 [[抛物线]] 的焦点性质混淆。",
        )
    ).encode()


async def _projection_row(
    session_factory: async_sessionmaker[AsyncSession], user_id: UUID, memory_id: str
) -> dict[str, Any] | None:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT title, summary, aliases, keywords, related_topic_keys, search_text, "
                "source_version FROM memory_index_entries "
                "WHERE user_id = :u AND memory_id = :m"
            ),
            {"u": user_id, "m": memory_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def _seed_document(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    *,
    user_id: UUID,
    memory_id: str,
    memory_type: str,
    content: bytes,
    topic_key: str | None = None,
) -> str:
    """写入真实版本文件 + current 副本 + memory_documents 指针；返回 storage_key。"""
    stored = await store.write_immutable_version(
        user_id=user_id, memory_id=memory_id, version=1, content=content
    )
    await store.materialize_current(user_id=user_id, memory_id=memory_id, content=content)
    async with session_factory() as session:
        async with session.begin():
            await docs_repo.upsert_document(
                session,
                user_id=user_id,
                memory_id=memory_id,
                memory_type=memory_type,
                topic_key=topic_key,
                topic_title=topic_key,
                logical_path=logical_path_for(memory_id),
            )
            await docs_repo.set_active_version(
                session,
                user_id=user_id,
                memory_id=memory_id,
                active_version=1,
                active_storage_key=stored.storage_key,
                active_checksum=stored.checksum,
            )
    return stored.storage_key


async def _active(session_factory: async_sessionmaker[AsyncSession], user_id: UUID, memory_id: str):
    async with session_factory() as session:
        return await docs_repo.get_document(session, user_id=user_id, memory_id=memory_id)


async def _run_migration(
    session_factory: async_sessionmaker[AsyncSession],
    runtime_context: MemoryRuntimeContext,
    *,
    batch_size: int = 100,
    cursor: str | None = None,
    dry_run: bool = False,
    idem_suffix: str,
) -> dict[str, Any]:
    """Scheduler 侧建 run + 关联 operation，经 Graph 执行并返回 detail。"""
    operation = make_operation(
        user_id=UUID(SYSTEM_MAINTENANCE_USER_ID),
        actor_type="system",
        input_kind="maintenance",
        operation_type="migrate_markdown_schema_v2",
        priority=0,
        payload=MaintenanceCommand(
            kind="migrate_markdown_schema_v2",
            batch_size=batch_size,
            cursor=cursor,
            dry_run=dry_run,
        ),
    )
    await persist_operation(session_factory, operation)
    async with session_factory() as session:
        async with session.begin():
            run, _created = await maintenance_repo.create_or_reuse_run(
                session,
                run_id=uuid4(),
                maintenance_type="migrate_markdown_schema_v2",
                idempotency_key=f"migrate-schema-v2:it-{idem_suffix}",
            )
            await maintenance_repo.attach_operation(
                session, run_id=run["run_id"], operation_id=operation.operation_id
            )
    runner = LocalLangGraphRunner(context=runtime_context)
    result = await runner.run(operation)
    assert result.status == "succeeded", result.error
    # detail 落在 maintenance run 行上（与 verify_checksums 测试同源），不在 operation 结果里
    async with session_factory() as session:
        run_row = await maintenance_repo.get_run_by_key(
            session, idempotency_key=f"migrate-schema-v2:it-{idem_suffix}"
        )
    assert run_row is not None
    return dict(run_row["result"] or {})


# ---------------------------------------------------------------------------
# 升级与历史不可变
# ---------------------------------------------------------------------------


async def test_migration_upgrades_v1_documents_and_keeps_history_byte_identical(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    runtime_context: MemoryRuntimeContext,
) -> None:
    """v1 → v2：追加新版本、current 变 v2，**历史 versions 文件字节级不变**。"""
    user_id, mastery_id = uuid4(), "mastery:椭圆"
    learner_v1 = await _seed_document(
        session_factory,
        store,
        user_id=user_id,
        memory_id="learner",
        memory_type="learner",
        content=_v1_learner(user_id),
    )
    mastery_v1 = await _seed_document(
        session_factory,
        store,
        user_id=user_id,
        memory_id=mastery_id,
        memory_type="mastery",
        content=_v1_mastery(user_id, "椭圆"),
        topic_key="椭圆",
    )
    # 迁移前快照：历史版本字节与 checksum
    before_learner = await store.read_version(user_id=user_id, storage_key=learner_v1)
    before_mastery = await store.read_version(user_id=user_id, storage_key=mastery_v1)

    detail = await _run_migration(session_factory, runtime_context, idem_suffix=f"{user_id}-first")
    assert detail["migrated"] >= 2, detail

    # 1) 活动指针推进到 v2
    learner_doc = await _active(session_factory, user_id, "learner")
    mastery_doc = await _active(session_factory, user_id, mastery_id)
    assert learner_doc is not None and learner_doc["active_version"] == 2
    assert mastery_doc is not None and mastery_doc["active_version"] == 2

    # 2) current 已是 v2 且能解析
    current_learner = await store.read_current(user_id=user_id, memory_id="learner")
    current_mastery = await store.read_current(user_id=user_id, memory_id=mastery_id)
    assert parse_learner(current_learner.decode()).schema_version == SCHEMA_VERSION_V2
    upgraded = parse_mastery(current_mastery.decode())
    assert upgraded.schema_version == SCHEMA_VERSION_V2
    assert upgraded.name == "椭圆"
    assert upgraded.links == ["抛物线"], "迁移必须从正文现算 [[link]]"

    # 3) **历史 versions 文件字节级不变**（§5.6 铁律）
    assert await store.read_version(user_id=user_id, storage_key=learner_v1) == before_learner
    assert await store.read_version(user_id=user_id, storage_key=mastery_v1) == before_mastery
    # 新活动版本的 checksum 必须与实际文件一致（immutable write 语义）
    new_learner_bytes = await store.read_version(
        user_id=user_id, storage_key=learner_doc["active_storage_key"]
    )
    assert sha256_hex(new_learner_bytes) == learner_doc["active_checksum"]
    assert new_learner_bytes != before_learner, "current 内容确实变了，但走的是追加新版本"


async def test_second_migration_is_idempotent(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    runtime_context: MemoryRuntimeContext,
) -> None:
    """二次迁移：无新版本、无 checksum 抖动、无重复副作用。"""
    user_id = uuid4()
    await _seed_document(
        session_factory,
        store,
        user_id=user_id,
        memory_id="learner",
        memory_type="learner",
        content=_v1_learner(user_id),
    )

    await _run_migration(session_factory, runtime_context, idem_suffix=f"{user_id}-a")
    first = await _active(session_factory, user_id, "learner")
    assert first is not None
    first_version = first["active_version"]
    first_checksum = first["active_checksum"]
    first_key = first["active_storage_key"]

    detail = await _run_migration(session_factory, runtime_context, idem_suffix=f"{user_id}-b")
    assert detail["migrated"] == 0, "已是 v2 不得再升版"
    assert detail["skipped_already_v2"] >= 1
    assert detail["failures"] == []

    second = await _active(session_factory, user_id, "learner")
    assert second is not None
    assert second["active_version"] == first_version, "版本号不得抖动"
    assert second["active_checksum"] == first_checksum, "checksum 不得抖动"
    assert second["active_storage_key"] == first_key


async def test_second_run_backfills_projection_of_already_v2_documents(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    runtime_context: MemoryRuntimeContext,
) -> None:
    """review-2 新发现 9：**已是 v2** 的文档也必须被回填投影（无论何时跑迁移）。

    文档升到 v2 有两条路：本任务（v1→v2 追加版本）与 ``frontmatter_patch`` 原生升版。
    旧实现只在"本次升级了文档"的分支刷新投影，于是后者（以及被更早版本迁移跳过的文档）
    的 aliases/keywords/related 永远补不上，运维"再跑一次任务"是 no-op。
    """
    user_id, mastery_id = uuid4(), "mastery:椭圆"
    v2_bytes = _v2_mastery(user_id, "椭圆")
    await _seed_document(
        session_factory,
        store,
        user_id=user_id,
        memory_id=mastery_id,
        memory_type="mastery",
        content=v2_bytes,
        topic_key="椭圆",
    )
    # 模拟"v2 文档但投影从未回填"：投影行缺失
    assert await _projection_row(session_factory, user_id, mastery_id) is None

    detail = await _run_migration(session_factory, runtime_context, idem_suffix=f"{user_id}-v2")
    assert detail["migrated"] == 0, "已是 v2：不产生新版本"
    assert detail["skipped_already_v2"] >= 1
    assert detail["refreshed_already_v2"] >= 1, "已是 v2 的文档必须被回填投影"
    assert detail["failures"] == []

    row = await _projection_row(session_factory, user_id, mastery_id)
    assert row is not None, "迁移必须补齐 memory_index_entries 投影行"
    assert row["source_version"] == 1
    assert row["title"] == "椭圆"
    assert list(row["aliases"]) == ["ellipse"]
    assert list(row["keywords"]) == ["焦点", "准线"]
    assert list(row["related_topic_keys"]) == ["抛物线"], "链接必须从正文现算"
    # 文档本体不受影响：没有新版本、旧版本字节不变
    doc = await _active(session_factory, user_id, mastery_id)
    assert doc is not None and doc["active_version"] == 1
    assert await store.read_version(user_id=user_id, storage_key=doc["active_storage_key"]) == (
        v2_bytes
    )
    # index dirty 被标上（rebuild_index 的触发条件）
    async with session_factory() as session:
        dirty = await session.execute(
            text(
                "SELECT index_dirty_at FROM memory_documents "
                "WHERE user_id = :u AND memory_id = 'index'"
            ),
            {"u": user_id},
        )
        assert dirty.scalar_one() is not None


async def test_projection_backfill_is_idempotent_across_runs(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    runtime_context: MemoryRuntimeContext,
) -> None:
    """回填可重复跑：第二次不报错、投影内容不变（便于运维按需重跑）。"""
    user_id, mastery_id = uuid4(), "mastery:椭圆"
    await _seed_document(
        session_factory,
        store,
        user_id=user_id,
        memory_id=mastery_id,
        memory_type="mastery",
        content=_v2_mastery(user_id, "椭圆"),
        topic_key="椭圆",
    )
    await _run_migration(session_factory, runtime_context, idem_suffix=f"{user_id}-first")
    first = await _projection_row(session_factory, user_id, mastery_id)
    assert first is not None

    detail = await _run_migration(session_factory, runtime_context, idem_suffix=f"{user_id}-again")
    assert detail["refreshed_already_v2"] >= 1
    again = await _projection_row(session_factory, user_id, mastery_id)
    assert again is not None
    for key in ("title", "summary", "aliases", "keywords", "related_topic_keys", "search_text"):
        assert again[key] == first[key], f"{key} 在重跑后发生变化"
    doc = await _active(session_factory, user_id, mastery_id)
    assert doc is not None and doc["active_version"] == 1, "回填不得推进版本"


# ---------------------------------------------------------------------------
# dry_run 与坏文档
# ---------------------------------------------------------------------------


async def test_dry_run_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    runtime_context: MemoryRuntimeContext,
) -> None:
    user_id = uuid4()
    await _seed_document(
        session_factory,
        store,
        user_id=user_id,
        memory_id="learner",
        memory_type="learner",
        content=_v1_learner(user_id),
    )
    before = await store.read_current(user_id=user_id, memory_id="learner")

    detail = await _run_migration(
        session_factory, runtime_context, dry_run=True, idem_suffix=f"{user_id}-dry"
    )
    assert detail["dry_run"] is True
    assert detail["migrated"] >= 1, "dry-run 仍应报告将迁移多少条"

    after_doc = await _active(session_factory, user_id, "learner")
    assert after_doc is not None and after_doc["active_version"] == 1, "dry-run 不得推进版本"
    assert await store.read_current(user_id=user_id, memory_id="learner") == before


async def test_broken_document_does_not_block_others(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    runtime_context: MemoryRuntimeContext,
) -> None:
    """一个坏文档进 failures，同批其他用户的文档照常迁移（§5.6 验收）。"""
    broken_user, healthy_user = uuid4(), uuid4()
    # 坏文档：内容不是合法 markdown（但仍登记为 active，模拟历史损坏）
    bad_bytes = b"not a markdown document at all"
    stored = await store.write_immutable_version(
        user_id=broken_user, memory_id="learner", version=1, content=bad_bytes
    )
    await store.materialize_current(user_id=broken_user, memory_id="learner", content=bad_bytes)
    async with session_factory() as session:
        async with session.begin():
            await docs_repo.upsert_document(
                session,
                user_id=broken_user,
                memory_id="learner",
                memory_type="learner",
                topic_key=None,
                topic_title=None,
                logical_path=logical_path_for("learner"),
            )
            await docs_repo.set_active_version(
                session,
                user_id=broken_user,
                memory_id="learner",
                active_version=1,
                active_storage_key=stored.storage_key,
                active_checksum=stored.checksum,
            )
    await _seed_document(
        session_factory,
        store,
        user_id=healthy_user,
        memory_id="learner",
        memory_type="learner",
        content=_v1_learner(healthy_user),
    )

    detail = await _run_migration(
        session_factory, runtime_context, idem_suffix=f"{broken_user}-mix"
    )
    assert detail["failures"], "坏文档必须被记录而不是静默跳过"
    assert any("parse_failed" in reason for f in detail["failures"] for reason in f["reasons"])

    broken_doc = await _active(session_factory, broken_user, "learner")
    healthy_doc = await _active(session_factory, healthy_user, "learner")
    assert broken_doc is not None and broken_doc["active_version"] == 1, "坏文档保持原状"
    assert healthy_doc is not None and healthy_doc["active_version"] == 2, "好文档不受影响"


# ---------------------------------------------------------------------------
# 有界 batch 与 cursor 续跑
# ---------------------------------------------------------------------------


async def test_bounded_batch_returns_continue_with_cursor(
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalMarkdownStore,
    runtime_context: MemoryRuntimeContext,
) -> None:
    """batch_size=1 时跑不完 → status=continue + next_cursor，下次从游标继续。"""
    user_id = uuid4()
    await _seed_document(
        session_factory,
        store,
        user_id=user_id,
        memory_id="learner",
        memory_type="learner",
        content=_v1_learner(user_id),
    )
    await _seed_document(
        session_factory,
        store,
        user_id=user_id,
        memory_id="mastery:椭圆",
        memory_type="mastery",
        content=_v1_mastery(user_id, "椭圆"),
        topic_key="椭圆",
    )

    first = await _run_migration(
        session_factory, runtime_context, batch_size=1, idem_suffix=f"{user_id}-cur1"
    )
    assert first["status"] == "continue"
    assert first["next_cursor"], "未跑完必须给出续跑游标"
    assert len(first.get("failures", [])) == 0

    second = await _run_migration(
        session_factory,
        runtime_context,
        batch_size=1,
        cursor=first["next_cursor"],
        idem_suffix=f"{user_id}-cur2",
    )
    assert second["migrated"] + second["skipped_already_v2"] >= 1, "续跑必须真的处理了后续行"
