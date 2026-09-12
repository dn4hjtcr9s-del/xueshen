"""Turn 重试 / 跨节点恢复时的段登记（I-1 修复的验收测试）。

真实 PostgreSQL + 真实文件系统，覆盖 review §4 I-1 的两条路径：

- **重试**：turn 第一次尝试已 ``close_turn``（图抛异常 → finally 封存）→ Worker 重新
  claim 重试。旧实现里重试新建段会撞 ``uq_rollout_segment_turn_active``，
  ``register_open`` 把异常吞成 warning，于是新段没有 manifest 行，``seal()`` 更新 0 行
  但对象已上传 → 孤儿对象 + 指针全部落空。
- **跨节点恢复**：本地热段文件缺失 → 旧行标 ``deleted`` → 新段起点由 manifest 给出，
  不再撞 ``uq_rollout_segment_thread_ordinal``（0009 把它也改成部分唯一索引）。

另含：把 ``register_open`` 打成失败必须**可判定**降级（不写未登记段、不产生孤儿对象），
以及迁移 ``0009`` 在临时库上的四条真实路径（空库 upgrade / 空库 downgrade /
有数据 upgrade / 有数据 downgrade 守卫）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation import metrics
from backend.conversation.graph.state import SystemClock, SystemIdGenerator
from backend.conversation.persistence import rollout_manifests as manifests_repo
from backend.conversation.rollout.file_naming import (
    list_segment_paths,
    segment_dir,
    segment_path,
)
from backend.conversation.rollout.object_store import LocalRolloutObjectStore
from backend.conversation.rollout.recorder import RolloutRecorder, read_last_ordinal
from backend.conversation.rollout.sealer import (
    RegistrationStatus,
    RolloutSegmentSealer,
)

pytest_plugins = ("tests.conversation.conftest",)

_NOW = datetime(2026, 9, 10, 5, 0, 0, tzinfo=UTC)
_LEASE_OWNER = "worker-1"
_LEASE_GENERATION = 3


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


async def _seed_turn(
    factory: async_sessionmaker[AsyncSession], *, message_count: int = 1
) -> dict[str, Any]:
    """插入 thread + running turn + N 条消息，返回它们的 id。"""
    thread_id, user_id = uuid.uuid4(), uuid.uuid4()
    turn_id = uuid.uuid4()
    message_ids = [uuid.uuid4() for _ in range(message_count)]
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_threads (thread_id, user_id) "
                    "VALUES (:thread_id, :user_id)"
                ),
                {"thread_id": thread_id, "user_id": user_id},
            )
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_turns ("
                    "turn_id, thread_id, user_id, client_request_id, request_id, run_id, "
                    "user_message_id, status, lease_owner, lease_generation, "
                    "next_attempt_at, expected_thread_version"
                    ") VALUES ("
                    ":turn_id, :thread_id, :user_id, 'c-1', 'req-1', 'run-1', "
                    ":user_message_id, 'running', :lease_owner, :lease_generation, :now, 1)"
                ),
                {
                    "turn_id": turn_id,
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "user_message_id": message_ids[0],
                    "lease_owner": _LEASE_OWNER,
                    "lease_generation": _LEASE_GENERATION,
                    "now": _NOW,
                },
            )
            for index, message_id in enumerate(message_ids, start=1):
                content = f"第 {index} 条消息"
                await session.execute(
                    text(
                        "INSERT INTO conversation.conversation_messages ("
                        "message_id, thread_id, turn_id, user_id, sequence, role, content, "
                        "status, content_hash, occurred_at"
                        ") VALUES ("
                        ":message_id, :thread_id, :turn_id, :user_id, :sequence, 'user', "
                        ":content, 'completed', :content_hash, :now)"
                    ),
                    {
                        "message_id": message_id,
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "user_id": user_id,
                        "sequence": index,
                        "content": content,
                        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                        "now": _NOW,
                    },
                )
    return {
        "thread_id": thread_id,
        "user_id": user_id,
        "turn_id": turn_id,
        "message_ids": message_ids,
    }


def _build_recorder(
    factory: async_sessionmaker[AsyncSession], root: Path, object_store: Any
) -> RolloutRecorder:
    """按生产装配构造 recorder（真 sealer + 真对象存储 + 真文件系统）。"""
    return RolloutRecorder(
        root=root,
        clock=SystemClock(),
        id_generator=SystemIdGenerator(),
        logger=logging.getLogger("test.rollout.seg_retry"),
        sealer=RolloutSegmentSealer(
            session_factory=factory,
            object_store=object_store,
            logger=logging.getLogger("test.rollout.seg_retry"),
        ),
    )


async def _record_messages(
    recorder: RolloutRecorder,
    ids: dict[str, Any],
    *,
    fence: tuple[str, int] | None = None,
    message_indexes: list[int] | None = None,
    turn_id: uuid.UUID | None = None,
) -> None:
    """打开段 → 写若干条消息记录 → 关闭（触发封存）。"""
    handle = await recorder.open_turn(
        thread_id=ids["thread_id"],
        turn_id=turn_id if turn_id is not None else ids["turn_id"],
        user_id=ids["user_id"],
        thread_created_at=_NOW,
        fence=fence if fence is not None else (_LEASE_OWNER, _LEASE_GENERATION),
    )
    assert handle is not None, "open_turn 不应返回 None"
    indexes = message_indexes if message_indexes is not None else range(len(ids["message_ids"]))
    for index in indexes:
        content = f"第 {index + 1} 条消息"
        await recorder.record(
            "user_message",
            payload={
                "message_id": str(ids["message_ids"][index]),
                "sequence": index + 1,
                "role": "user",
                "content": content,
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                "occurred_at": _NOW.isoformat(),
            },
        )
    await recorder.close_turn()


async def _segment_rows(
    factory: async_sessionmaker[AsyncSession], turn_id: uuid.UUID
) -> list[dict[str, Any]]:
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT segment_id, ordinal_start, ordinal_end, status, object_key "
                        "FROM conversation.conversation_rollout_segments "
                        "WHERE turn_id = :turn_id ORDER BY ordinal_start, status"
                    ),
                    {"turn_id": turn_id},
                )
            )
            .mappings()
            .all()
        )
    return [dict(row) for row in rows]


async def _message_row(
    factory: async_sessionmaker[AsyncSession], message_id: uuid.UUID
) -> dict[str, Any]:
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT segment_id, rollout_ordinal, rollout_byte_offset_start, "
                        "rollout_byte_offset_end, content "
                        "FROM conversation.conversation_messages WHERE message_id = :message_id"
                    ),
                    {"message_id": message_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


def _hot_files(root: Path, thread_id: uuid.UUID) -> list[Path]:
    directory = segment_dir(root=root, thread_created_at=_NOW, thread_id=thread_id)
    return [item[2] for item in list_segment_paths(directory)]


# ---------------------------------------------------------------------------
# 重试：复用既有段
# ---------------------------------------------------------------------------


async def test_retry_after_seal_registers_reuses_and_seals(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """第一次尝试已封存 → 重试必须能登记 + 推进 ``seal()``，且不产生孤儿对象。"""
    factory = conversation_session_factory
    ids = await _seed_turn(factory, message_count=2)
    object_store = LocalRolloutObjectStore(root=tmp_path)
    recorder = _build_recorder(factory, tmp_path, object_store)

    # 第一次尝试：正常封存（对应"图抛异常 → finally: close_turn()"）
    await _record_messages(recorder, ids, message_indexes=[0])
    first = await _segment_rows(factory, ids["turn_id"])
    assert [(row["status"], row["ordinal_start"]) for row in first] == [("sealed", 0)]
    first_segment_id = first[0]["segment_id"]
    first_object_key = first[0]["object_key"]
    assert first_object_key is not None
    first_end = first[0]["ordinal_end"]
    assert first_end is not None

    # Worker 重新 claim → 重试：同一 turn、同一段序号范围
    await _record_messages(recorder, ids, message_indexes=[0, 1])
    rows = await _segment_rows(factory, ids["turn_id"])
    assert len(rows) == 2, "旧行保留 tombstone，新行继承序号范围"
    superseded, active = rows
    assert superseded["segment_id"] == first_segment_id
    assert superseded["status"] == "deleted"
    assert superseded["ordinal_start"] == 0
    assert active["status"] == "sealed"
    assert active["segment_id"] != first_segment_id, (
        "新段必须换 id（否则对象 key 相同、不可变写冲突）"
    )
    assert active["ordinal_start"] == 0, "复用同一 ordinal_start（本地热段文件继续追加）"
    assert active["ordinal_end"] is not None and active["ordinal_end"] > first_end, (
        "重试写入了新记录，ordinal_end 必须前进"
    )
    assert active["object_key"] not in (None, first_object_key)

    # 热文件只有一个（旧文件已随登记改名到新段 id），内容是两次尝试的全部记录：
    # thread_meta + 第一次尝试的 user_message + 重试的 user_message。
    hot = _hot_files(tmp_path, ids["thread_id"])
    assert len(hot) == 1
    assert (
        hot[0].name
        == segment_path(
            root=tmp_path,
            thread_created_at=_NOW,
            thread_id=ids["thread_id"],
            ordinal_start=0,
            segment_id=active["segment_id"],
        ).name
    )
    hot_records = hot[0].read_bytes()
    assert hot_records.count(b'"type":"thread_meta"') == 1, "续写不得重复写 thread_meta"
    assert hot_records.count(b'"type":"user_message"') == 3, (
        "第一次尝试的 1 条 + 重试的 2 条（重放同一条消息会再写一行，指针随后覆盖）"
    )

    # 指针：指向**当前活跃段**（不是被标废的旧段），且能按区间取回正文
    message = await _message_row(factory, ids["message_ids"][0])
    assert message["segment_id"] == active["segment_id"]
    assert message["rollout_ordinal"] == 2, "续写段从旧段末序号继续分配"
    data = await object_store.get(key=str(active["object_key"]))
    raw = data[message["rollout_byte_offset_start"] : message["rollout_byte_offset_end"]]
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert message["content"].encode() in raw

    # 两个对象都有主人：第一个对象对应被标废的旧段行（保留审计与旧对象引用），
    # 不是"已上传却无人登记"的孤儿。
    all_objects = {stat.key for stat in await object_store.list_prefix(prefix="rollouts/")}
    assert all_objects == {str(first_object_key), str(active["object_key"])}

    await recorder.aclose()


async def test_retry_without_new_records_keeps_previous_manifest(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """重试没有产生任何新记录时：不得把已封存段改成空 open 行。"""
    factory = conversation_session_factory
    ids = await _seed_turn(factory)
    object_store = LocalRolloutObjectStore(root=tmp_path)
    recorder = _build_recorder(factory, tmp_path, object_store)

    await _record_messages(recorder, ids)
    before = await _segment_rows(factory, ids["turn_id"])

    await recorder.open_turn(
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        user_id=ids["user_id"],
        thread_created_at=_NOW,
        fence=(_LEASE_OWNER, _LEASE_GENERATION),
    )
    await recorder.close_turn()

    after = await _segment_rows(factory, ids["turn_id"])
    assert after == before, "空重试不得改变 manifest（不留空 open 行、不标废已封存段）"
    assert len(await object_store.list_prefix(prefix="rollouts/")) == 1
    await recorder.aclose()


async def test_retry_registration_failure_never_uploads_orphan_object(
    conversation_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """``register_open`` 打失败时必须降级：不封存、不上传对象、留可观测标记。"""
    factory = conversation_session_factory
    ids = await _seed_turn(factory)
    object_store = LocalRolloutObjectStore(root=tmp_path)
    recorder = _build_recorder(factory, tmp_path, object_store)

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("模拟 manifest 登记失败")

    monkeypatch.setattr(manifests_repo, "insert_open", _boom)
    before = metrics.rollout_segment_registration_total.labels(result="failed")._value.get()

    await _record_messages(recorder, ids)

    assert recorder.degraded is True, "登记失败必须让 recorder 进入降级"
    after = metrics.rollout_segment_registration_total.labels(result="failed")._value.get()
    # 每条待写行都会重试登记，因此计数按"失败次数"增长，而不是恰好 1 次
    assert after > before, "登记失败必须打指标（不能只留一行 warning）"
    rows = await _segment_rows(factory, ids["turn_id"])
    assert rows == [], "没有 manifest 行时不得留下任何段行"
    objects = await object_store.list_prefix(prefix="rollouts/")
    assert objects == [], "没有 manifest 行时绝不上传对象（否则就是孤儿对象）"
    # 第一行在"登记成功"之前不会落盘，因此连热段文件都不该存在：
    # 既没有孤儿对象，也没有无登记的残留文件。
    assert _hot_files(tmp_path, ids["thread_id"]) == []
    await recorder.aclose()


# ---------------------------------------------------------------------------
# 跨节点恢复
# ---------------------------------------------------------------------------


async def test_cross_node_recovery_marks_old_deleted_and_registers_new(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """本地文件缺失：旧段标 deleted，新段用新起点登记（0009 的部分唯一索引）。"""
    factory = conversation_session_factory
    ids = await _seed_turn(factory, message_count=2)
    object_store = LocalRolloutObjectStore(root=tmp_path)

    # 节点 A：写了记录但没来得及 close_turn（崩溃）→ 段停在 open，热文件留在 A
    node_a = _build_recorder(factory, tmp_path, object_store)
    handle = await node_a.open_turn(
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        user_id=ids["user_id"],
        thread_created_at=_NOW,
        fence=(_LEASE_OWNER, _LEASE_GENERATION),
    )
    assert handle is not None
    first_segment_id = handle.segment_id
    first_ordinal_start = handle.ordinal_start
    await _record_messages_payload_only(node_a, ids, message_indexes=[0])
    await node_a.flush()
    assert (await _segment_rows(factory, ids["turn_id"]))[0]["status"] == "open"

    # 节点 B：完全不同的根目录 → 本地热段文件不在
    node_b_root = tmp_path / "node-b"
    node_b = _build_recorder(factory, node_b_root, object_store)
    await _record_messages(node_b, ids, message_indexes=[0, 1])

    rows = await _segment_rows(factory, ids["turn_id"])
    assert len(rows) == 2
    stale, fresh = rows
    assert stale["segment_id"] == first_segment_id
    assert stale["status"] == "deleted", "跨节点时旧行必须标 tombstone"
    assert fresh["status"] == "sealed"
    assert fresh["segment_id"] != first_segment_id
    assert fresh["ordinal_start"] >= first_ordinal_start, "新段起点不得与旧段范围冲突"
    assert fresh["object_key"] is not None

    # 新段落在节点 B 的目录下，节点 A 的热文件仍在（跨节点不共享本地盘）
    node_b_files = _hot_files(node_b_root, ids["thread_id"])
    assert len(node_b_files) == 1
    assert (
        node_b_files[0].parent
        == segment_path(
            root=node_b_root,
            thread_created_at=_NOW,
            thread_id=ids["thread_id"],
            ordinal_start=int(fresh["ordinal_start"]),
            segment_id=fresh["segment_id"],
        ).parent
    )
    assert len(_hot_files(tmp_path, ids["thread_id"])) == 1, "节点 A 的热文件不受影响"

    # 指针落到新段，且新段能被读者按区间取回正文
    message = await _message_row(factory, ids["message_ids"][0])
    assert message["segment_id"] == fresh["segment_id"]
    data = await object_store.get(key=str(fresh["object_key"]))
    raw = data[message["rollout_byte_offset_start"] : message["rollout_byte_offset_end"]]
    assert message["content"].encode() in raw

    await node_a.aclose()
    await node_b.aclose()


async def test_same_ordinal_start_coexists_with_deleted_row(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """约束级验收：``(thread_id, ordinal_start)`` 只对非 deleted 行唯一（迁移 0009）。

    0008 之后 ``turn_id`` 已放宽，但 ``ordinal_start`` 仍是覆盖 deleted 行的硬约束，
    "标废旧段 + 复用同一序号" 必然抛 ``uq_rollout_segment_thread_ordinal``。
    """
    factory = conversation_session_factory
    ids = await _seed_turn(factory, message_count=2)
    thread_id = ids["thread_id"]
    deleted_segment, active_segment = uuid.uuid4(), uuid.uuid4()

    async with factory() as session:
        async with session.begin():
            # 1 deleted + 1 活跃，同一 (thread_id, ordinal_start)
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_rollout_segments ("
                    "segment_id, thread_id, turn_id, ordinal_start, status, deleted_at"
                    ") VALUES (:segment_id, :thread_id, :turn_id, 0, 'deleted', :now)"
                ),
                {
                    "segment_id": deleted_segment,
                    "thread_id": thread_id,
                    "turn_id": ids["turn_id"],
                    "now": _NOW,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_rollout_segments ("
                    "segment_id, thread_id, turn_id, ordinal_start, status"
                    ") VALUES (:segment_id, :thread_id, :turn_id, 0, 'open')"
                ),
                {
                    "segment_id": active_segment,
                    "thread_id": thread_id,
                    "turn_id": uuid.uuid4(),
                },
            )
        # 第二个非 deleted 行必须仍被拒（部分唯一索引对活跃行依然生效）
        async with session.begin_nested() as savepoint:
            rejected = False
            try:
                await session.execute(
                    text(
                        "INSERT INTO conversation.conversation_rollout_segments ("
                        "segment_id, thread_id, turn_id, ordinal_start, status"
                        ") VALUES (:segment_id, :thread_id, :turn_id, 0, 'open')"
                    ),
                    {
                        "segment_id": uuid.uuid4(),
                        "thread_id": thread_id,
                        "turn_id": uuid.uuid4(),
                    },
                )
            except Exception:
                rejected = True
                await savepoint.rollback()
        assert rejected, "两个非 deleted 段不得共享同一 ordinal_start"

    async with factory() as session:
        rows = await manifests_repo.list_by_thread(session, thread_id, include_deleted=True)
    assert sorted(row["status"] for row in rows) == ["deleted", "open"]

    # 旧的 open 段被标废后，同一 ordinal_start 可以立刻被新段复用（0009 的部分唯一索引）
    async with factory() as session:
        async with session.begin():
            assert await manifests_repo.mark_deleted(session, segment_id=active_segment) is True
            assert (
                await manifests_repo.insert_open(
                    session,
                    segment_id=uuid.uuid4(),
                    thread_id=thread_id,
                    turn_id=ids["turn_id"],
                    ordinal_start=0,
                )
                is True
            ), "标废旧行后必须能用同一 ordinal_start 登记新段"
        async with session.begin():
            # 同一 segment_id 重放仍幂等（不给同一个 id 插第二行）
            current = (
                await session.execute(
                    text(
                        "SELECT segment_id FROM conversation.conversation_rollout_segments "
                        "WHERE thread_id = :thread_id AND status = 'open'"
                    ),
                    {"thread_id": thread_id},
                )
            ).scalar_one()
            assert (
                await manifests_repo.insert_open(
                    session,
                    segment_id=current,
                    thread_id=thread_id,
                    turn_id=ids["turn_id"],
                    ordinal_start=0,
                )
                is False
            )
            assert await manifests_repo.next_ordinal_start(session, thread_id) == 1


async def _record_messages_payload_only(
    recorder: RolloutRecorder,
    ids: dict[str, Any],
    *,
    message_indexes: list[int],
) -> None:
    """只写记录、不关段（模拟崩溃前的写入）。"""
    for index in message_indexes:
        content = f"第 {index + 1} 条消息"
        await recorder.record(
            "user_message",
            payload={
                "message_id": str(ids["message_ids"][index]),
                "sequence": index + 1,
                "role": "user",
                "content": content,
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                "occurred_at": _NOW.isoformat(),
            },
        )


# ---------------------------------------------------------------------------
# 登记结果的结构化返回
# ---------------------------------------------------------------------------


async def test_register_open_reports_insert_and_replay(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """``register_open`` 必须返回可判定的状态，而不是"要么成功要么吞异常"。"""
    factory = conversation_session_factory
    ids = await _seed_turn(factory)
    sealer = RolloutSegmentSealer(
        session_factory=factory,
        object_store=LocalRolloutObjectStore(root=tmp_path),
        logger=logging.getLogger("test.rollout.seg_retry"),
    )
    segment_id = uuid.uuid4()
    first = await sealer.register_open(
        segment_id=segment_id,
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        ordinal_start=0,
    )
    assert first.status is RegistrationStatus.inserted
    assert first.ok is True

    replay = await sealer.register_open(
        segment_id=segment_id,
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        ordinal_start=0,
    )
    assert replay.status is RegistrationStatus.already_open, "重复登记同一 segment_id 幂等"

    conflict = await sealer.register_open(
        segment_id=uuid.uuid4(),
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        ordinal_start=0,
    )
    assert conflict.status is RegistrationStatus.failed, "同一 turn 第二个非删除段必须可判定失败"
    assert conflict.ok is False
    assert conflict.reason == "IntegrityError", "原因必须是可读的异常类名"

    rows = await _segment_rows(factory, ids["turn_id"])
    assert len(rows) == 1, "失败的登记不得留下半行"


# ---------------------------------------------------------------------------
# review-2 新发现 4①：新建段的 ordinal_start 必须避开未封存段已写过的序号
# ---------------------------------------------------------------------------


async def test_new_segment_avoids_unsealed_segment_ordinal_range(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """open 段的 ``ordinal_end`` 是 NULL → 只按 manifest 算会复用已写过的序号。

    复现 review-2 的实测：崩溃段文件里 ordinal 已用到 1，``next_ordinal_start`` 仍给 1，
    新段与旧段范围重叠（``(0,3)`` 与 ``(1,2)`` 那类），而没有任何 reconcile 类别能发现。
    """
    factory = conversation_session_factory
    ids = await _seed_turn(factory, message_count=1)
    object_store = LocalRolloutObjectStore(root=tmp_path)

    # 第一次尝试：写入两条记录（thread_meta=0、user_message=1）后"崩溃"
    # （flush + aclose：文件在、manifest 行停在 open、ordinal_end 仍为 NULL）
    crashed = _build_recorder(factory, tmp_path, object_store)
    handle = await crashed.open_turn(
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        user_id=ids["user_id"],
        thread_created_at=_NOW,
        fence=(_LEASE_OWNER, _LEASE_GENERATION),
    )
    assert handle is not None
    await _record_messages_payload_only(crashed, ids, message_indexes=[0])
    await crashed.flush()
    await crashed.aclose()

    rows = await _segment_rows(factory, ids["turn_id"])
    assert [(row["status"], row["ordinal_start"], row["ordinal_end"]) for row in rows] == [
        ("open", 0, None)
    ], "崩溃段必须停在 open 且 ordinal_end 为 NULL"
    files = _hot_files(tmp_path, ids["thread_id"])
    assert len(files) == 1
    assert read_last_ordinal(files[0]) == 1, "文件里已写到 ordinal=1"

    # manifest 侧只能看到 ordinal_start=0 → 旧实现会给出起点 1（重叠）
    async with factory() as session:
        assert await manifests_repo.next_ordinal_start(session, ids["thread_id"]) == 1, (
            "本断言固定 DB 侧的盲区：open 段的 ordinal_end 为 NULL"
        )

    # 新 turn（同一 thread，重启后）：起点必须避开已写过的序号
    other_turn = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            # 崩溃的 turn 在现实中会被 Worker 标成终态，之后才允许同 thread 开新 turn
            # （uq_conv_turns_one_active_per_thread 只允许一个活动 turn）
            await session.execute(
                text(
                    "UPDATE conversation.conversation_turns SET status = 'failed' "
                    "WHERE turn_id = :turn_id"
                ),
                {"turn_id": ids["turn_id"]},
            )
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_turns ("
                    "turn_id, thread_id, user_id, client_request_id, request_id, run_id, "
                    "user_message_id, status, lease_owner, lease_generation, "
                    "next_attempt_at, expected_thread_version"
                    ") VALUES ("
                    ":turn_id, :thread_id, :user_id, 'c-2', 'req-2', 'run-2', "
                    ":user_message_id, 'running', :lease_owner, :lease_generation, :now, 1)"
                ),
                {
                    "turn_id": other_turn,
                    "thread_id": ids["thread_id"],
                    "user_id": ids["user_id"],
                    "user_message_id": ids["message_ids"][0],
                    "lease_owner": _LEASE_OWNER,
                    "lease_generation": _LEASE_GENERATION,
                    "now": _NOW,
                },
            )

    recorder = _build_recorder(factory, tmp_path, object_store)
    handle = await recorder.open_turn(
        thread_id=ids["thread_id"],
        turn_id=other_turn,
        user_id=ids["user_id"],
        thread_created_at=_NOW,
        fence=(_LEASE_OWNER, _LEASE_GENERATION),
    )
    assert handle is not None
    assert handle.ordinal_start == 2, (
        "新段起点必须是「本地热段真实最大 ordinal + 1」，否则两段序号范围重叠"
    )
    await recorder.record(
        "user_message",
        payload={
            "message_id": str(ids["message_ids"][0]),
            "sequence": 1,
            "role": "user",
            "content": "第 1 条消息",
            "content_hash": hashlib.sha256("第 1 条消息".encode()).hexdigest(),
            "occurred_at": _NOW.isoformat(),
        },
    )
    await recorder.close_turn()
    await recorder.aclose()

    # 两段文件里的 ordinal 不得重复
    seen: list[int] = []
    for path in _hot_files(tmp_path, ids["thread_id"]):
        for line in path.read_bytes().splitlines():
            if line:
                seen.append(int(json.loads(line)["ordinal"]))
    assert len(seen) == len(set(seen)), f"ordinal 出现重复（范围重叠）：{sorted(seen)}"


# ---------------------------------------------------------------------------
# review-2 新发现 4②：热段改名失败必须补偿，不得谎称"没有 manifest 行"
# ---------------------------------------------------------------------------


async def test_rename_failure_compensates_and_next_turn_recovers(
    conversation_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改名失败：DB 已提交 → 补偿标废新行 + 清掉改名源，且**不**留下不可发现的残留。

    同时验证 review-2 新发现 5：这次降级只作用于本 turn，下一个 turn 必须能重新记录。
    """
    factory = conversation_session_factory
    ids = await _seed_turn(factory, message_count=1)
    object_store = LocalRolloutObjectStore(root=tmp_path)

    crashed = _build_recorder(factory, tmp_path, object_store)
    crash_handle = await crashed.open_turn(
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        user_id=ids["user_id"],
        thread_created_at=_NOW,
        fence=(_LEASE_OWNER, _LEASE_GENERATION),
    )
    assert crash_handle is not None
    await _record_messages_payload_only(crashed, ids, message_indexes=[0])
    await crashed.flush()
    await crashed.aclose()
    old_files = _hot_files(tmp_path, ids["thread_id"])
    assert len(old_files) == 1

    def _boom(source: Path, target: Path) -> None:
        raise OSError("注入的改名失败")

    recorder = _build_recorder(factory, tmp_path, object_store)
    monkeypatch.setattr("backend.conversation.rollout.sealer._rename_segment_file", _boom)

    # 同一 turn 重试 → 走"复用既有热段"路径（需要改名为新 segment_id 的路径）
    handle = await recorder.open_turn(
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        user_id=ids["user_id"],
        thread_created_at=_NOW,
        fence=(_LEASE_OWNER, _LEASE_GENERATION),
    )
    assert handle is not None
    await _record_messages_payload_only(recorder, ids, message_indexes=[0])
    await recorder.flush()

    assert recorder.degraded is True, "改名失败必须让记录器停止本 turn 的记录"
    rows = await _segment_rows(factory, ids["turn_id"])
    assert all(row["status"] == "deleted" for row in rows), (
        f"补偿后不得留下 open 行（DB 已变更却停止记录会留下不可发现的残留）：{rows}"
    )
    assert len(rows) == 2, "旧行（标废）+ 新行（补偿标废）各一条"
    assert not old_files[0].exists(), "改名源必须被清掉（否则成为不可发现的热文件残留）"
    assert _hot_files(tmp_path, ids["thread_id"]) == []
    assert await object_store.list_prefix(prefix="rollouts/") == [], "不得留下孤儿对象"
    # 生产路径由 runner 的 finally 调 close_turn：未登记的段不会封存，也不会写 manifest
    await recorder.close_turn()

    # 下一个 turn（同一个 recorder）：降级必须在 open_turn 时清零并重新可用
    monkeypatch.undo()
    other_turn = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            # 崩溃的 turn 在现实中会被 Worker 标成终态，之后才允许同 thread 开新 turn
            # （uq_conv_turns_one_active_per_thread 只允许一个活动 turn）
            await session.execute(
                text(
                    "UPDATE conversation.conversation_turns SET status = 'failed' "
                    "WHERE turn_id = :turn_id"
                ),
                {"turn_id": ids["turn_id"]},
            )
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_turns ("
                    "turn_id, thread_id, user_id, client_request_id, request_id, run_id, "
                    "user_message_id, status, lease_owner, lease_generation, "
                    "next_attempt_at, expected_thread_version"
                    ") VALUES ("
                    ":turn_id, :thread_id, :user_id, 'c-2', 'req-2', 'run-2', "
                    ":user_message_id, 'running', :lease_owner, :lease_generation, :now, 1)"
                ),
                {
                    "turn_id": other_turn,
                    "thread_id": ids["thread_id"],
                    "user_id": ids["user_id"],
                    "user_message_id": ids["message_ids"][0],
                    "lease_owner": _LEASE_OWNER,
                    "lease_generation": _LEASE_GENERATION,
                    "now": _NOW,
                },
            )
    await _record_messages(recorder, ids, turn_id=other_turn, message_indexes=[0])
    assert recorder.degraded is False, "降级必须收敛到单个 turn（新发现 5）"
    rows_after = await _segment_rows(factory, other_turn)
    assert [row["status"] for row in rows_after] == ["sealed"], "下一个 turn 必须正常封存"


# ---------------------------------------------------------------------------
# review-2 新发现 17：标废（tombstone）也必须带租约守卫
# ---------------------------------------------------------------------------


async def test_mark_deleted_and_discard_open_require_lease_fence(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """失租的 worker 不得 tombstone 当前持有者的段，也不得清空其消息指针。"""
    factory = conversation_session_factory
    ids = await _seed_turn(factory, message_count=1)
    object_store = LocalRolloutObjectStore(root=tmp_path)
    recorder = _build_recorder(factory, tmp_path, object_store)
    await _record_messages(recorder, ids, message_indexes=[0])
    rows = await _segment_rows(factory, ids["turn_id"])
    assert [row["status"] for row in rows] == ["sealed"]
    segment_id = rows[0]["segment_id"]
    pointer_before = await _message_row(factory, ids["message_ids"][0])
    assert pointer_before["segment_id"] == segment_id, "封存后消息指针应指向该段"

    # 租约不匹配：标废必须被拒绝，且**不能**清指针
    async with factory() as session:
        async with session.begin():
            denied = await manifests_repo.mark_deleted(
                session, segment_id=segment_id, fence=(_LEASE_OWNER, _LEASE_GENERATION + 1)
            )
    assert denied is False, "失租者不得 tombstone 当前持有者的段"
    assert (await _segment_rows(factory, ids["turn_id"]))[0]["status"] == "sealed"
    pointer_after = await _message_row(factory, ids["message_ids"][0])
    assert pointer_after["segment_id"] == segment_id, "被拒绝的标废不得清空消息指针"

    # 租约匹配：正常标废 + 清指针
    async with factory() as session:
        async with session.begin():
            allowed = await manifests_repo.mark_deleted(
                session, segment_id=segment_id, fence=(_LEASE_OWNER, _LEASE_GENERATION)
            )
    assert allowed is True
    assert (await _segment_rows(factory, ids["turn_id"]))[0]["status"] == "deleted"
    assert (await _message_row(factory, ids["message_ids"][0]))["segment_id"] is None

    # discard_open 同理（空段标废）
    sealer = RolloutSegmentSealer(
        session_factory=factory,
        object_store=object_store,
        logger=logging.getLogger("test.rollout.seg_retry"),
    )
    open_segment = uuid.uuid4()
    await sealer.register_open(
        segment_id=open_segment,
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        ordinal_start=99,
    )
    assert await sealer.discard_open(segment_id=open_segment, fence=("other-worker", 1)) is False
    assert (
        await sealer.discard_open(segment_id=open_segment, fence=(_LEASE_OWNER, _LEASE_GENERATION))
        is True
    )


# ---------------------------------------------------------------------------
# 迁移 0009 的真实路径（临时库，不改动 conversation_test 自身）
# ---------------------------------------------------------------------------

_MIGRATION_ACTIVE_INDEX = "uq_rollout_segment_thread_ordinal_active"
_MIGRATION_LEGACY_CONSTRAINT = "uq_rollout_segment_thread_ordinal"


def _require_docker() -> None:
    if shutil.which("docker") is None:
        pytest.skip("迁移路径测试需要 docker compose 提供的本地 PostgreSQL 管理权限")


def _admin_createdb(database: str, owner: str) -> None:
    admin_user = os.environ.get("POSTGRES_ADMIN_USER", "postgres")
    subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "postgres",
            "createdb",
            "-U",
            admin_user,
            "-O",
            owner,
            database,
        ],
        check=True,
        capture_output=True,
    )


def _admin_dropdb(database: str) -> None:
    admin_user = os.environ.get("POSTGRES_ADMIN_USER", "postgres")
    subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "dropdb", "-U", admin_user, database],
        check=False,
        capture_output=True,
    )


def _alembic(temp_url: str, *args: str) -> subprocess.CompletedProcess[str]:
    """在临时库上跑真实 alembic 命令（与 CI 同一条链路）。

    ``conversation_migrations/env.py`` 只认环境变量，因此这里显式覆盖；``UV_CACHE_DIR``
    继承当前进程，避免子进程写只读缓存目录。
    """
    environment = {**os.environ, "CONVERSATION_DATABASE_URL": temp_url}
    return subprocess.run(
        ["uv", "run", "alembic", "-c", "conversation_alembic.ini", *args],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


async def _index_names(factory: async_sessionmaker[AsyncSession]) -> set[str]:
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT indexname FROM pg_indexes "
                    "WHERE schemaname = 'conversation' "
                    "AND tablename = 'conversation_rollout_segments'"
                )
            )
        ).scalars()
    return set(rows)


async def _constraint_names(factory: async_sessionmaker[AsyncSession]) -> set[str]:
    async with factory() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = 'conversation.conversation_rollout_segments'::regclass"
                )
            )
        ).scalars()
    return set(rows)


async def _seed_migration_rows(
    factory: async_sessionmaker[AsyncSession],
    *,
    deleted_ordinal_start: int,
    active_ordinal_start: int,
) -> None:
    """造"1 deleted + 1 活跃"的段（跨节点重建留下的正常状态）。"""
    thread_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_rollout_segments ("
                    "segment_id, thread_id, turn_id, ordinal_start, status, deleted_at"
                    ") VALUES (:segment_id, :thread_id, :turn_id, :ordinal_start, 'deleted', :now)"
                ),
                {
                    "segment_id": uuid.uuid4(),
                    "thread_id": thread_id,
                    "turn_id": uuid.uuid4(),
                    "ordinal_start": deleted_ordinal_start,
                    "now": _NOW,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_rollout_segments ("
                    "segment_id, thread_id, turn_id, ordinal_start, ordinal_end, object_key, "
                    "object_etag, sha256, byte_size, status, sealed_at"
                    ") VALUES ("
                    ":segment_id, :thread_id, :turn_id, :ordinal_start, :ordinal_end, "
                    ":object_key, 'etag', :sha256, 8, 'sealed', :now)"
                ),
                {
                    "segment_id": uuid.uuid4(),
                    "thread_id": thread_id,
                    "turn_id": uuid.uuid4(),
                    "ordinal_start": active_ordinal_start,
                    "ordinal_end": active_ordinal_start + 2,
                    "object_key": f"rollouts/probe/{uuid.uuid4()}.jsonl",
                    "sha256": "a" * 64,
                    "now": _NOW,
                },
            )


@pytest.fixture()
def _migration_database(
    conversation_settings: Any,
) -> Any:
    """临时 conversation 库（管理员创建、归 conversation 角色），测试后删除。"""
    _require_docker()
    base_url = make_url(conversation_settings.conversation_database_url)
    database = f"conversation_test_mig_{uuid.uuid4().hex[:8]}"
    temp_url = base_url.set(database=database).render_as_string(hide_password=False)
    _admin_createdb(database, base_url.username or "conversation")
    try:
        yield temp_url
    finally:
        _admin_dropdb(database)


async def test_migration_0009_empty_database_round_trip(_migration_database: str) -> None:
    """空库：upgrade head → downgrade 0008 → upgrade head，三步都要成功。"""
    from backend.conversation.persistence.database import (
        create_conversation_engine,
        create_conversation_session_factory,
    )
    from backend.settings import Settings

    temp_url = _migration_database
    upgraded = await asyncio.to_thread(_alembic, temp_url, "upgrade", "head")
    assert upgraded.returncode == 0, upgraded.stderr

    engine = create_conversation_engine(
        Settings(app_env="test", conversation_database_url=temp_url)
    )
    factory = create_conversation_session_factory(engine)
    try:
        assert _MIGRATION_ACTIVE_INDEX in await _index_names(factory)
        assert _MIGRATION_LEGACY_CONSTRAINT not in await _constraint_names(factory)

        downgraded = await asyncio.to_thread(
            _alembic, temp_url, "downgrade", "0008_rollout_segment_turn_uq"
        )
        assert downgraded.returncode == 0, downgraded.stderr
        assert _MIGRATION_ACTIVE_INDEX not in await _index_names(factory)
        assert _MIGRATION_LEGACY_CONSTRAINT in await _constraint_names(factory)

        reupgraded = await asyncio.to_thread(_alembic, temp_url, "upgrade", "head")
        assert reupgraded.returncode == 0, reupgraded.stderr
        assert _MIGRATION_ACTIVE_INDEX in await _index_names(factory)
        assert _MIGRATION_LEGACY_CONSTRAINT not in await _constraint_names(factory)
    finally:
        await engine.dispose()


async def test_migration_0009_upgrade_with_data_and_downgrade_guard(
    _migration_database: str,
) -> None:
    """``0008`` 上带数据的 upgrade 成功；带"1 deleted + 1 活跃"回滚必须命中守卫。

    两个阶段都用真实 alembic 子进程：

    1. ``0008`` 上造普通数据（deleted 与活跃行序号不同，旧约束允许）→ ``upgrade head``
       必须成功且不丢行；
    2. 在 ``0009`` 下造出"同 thread 同 ``ordinal_start``：1 deleted + 1 活跃"——这正是
       旧 ``UNIQUE (thread_id, ordinal_start)`` 不允许、而新部分唯一索引允许的状态 →
       ``downgrade 0008`` 必须给出守卫的 ``RAISE EXCEPTION``（而不是撞 UniqueViolation），
       库仍停在新版本；清掉重复行后回滚成功。
    """
    from backend.conversation.persistence.database import (
        create_conversation_engine,
        create_conversation_session_factory,
    )
    from backend.settings import Settings

    temp_url = _migration_database
    base = await asyncio.to_thread(_alembic, temp_url, "upgrade", "0008_rollout_segment_turn_uq")
    assert base.returncode == 0, base.stderr

    engine = create_conversation_engine(
        Settings(app_env="test", conversation_database_url=temp_url)
    )
    factory = create_conversation_session_factory(engine)
    try:
        # ① 旧约束下的普通数据：序号不重叠
        await _seed_migration_rows(factory, deleted_ordinal_start=0, active_ordinal_start=5)
        upgraded = await asyncio.to_thread(_alembic, temp_url, "upgrade", "head")
        assert upgraded.returncode == 0, upgraded.stderr
        assert _MIGRATION_ACTIVE_INDEX in await _index_names(factory)
        async with factory() as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM conversation.conversation_rollout_segments")
                )
            ).scalar_one()
        assert count == 2, "upgrade 不得丢行"

        # ② 只有 0009 允许的状态：deleted 行与活跃行共享 ordinal_start
        await _seed_migration_rows(factory, deleted_ordinal_start=9, active_ordinal_start=9)

        # 回滚必须被守卫拒绝（而不是抛 UniqueViolation），且库仍停在新版本
        rejected = await asyncio.to_thread(
            _alembic, temp_url, "downgrade", "0008_rollout_segment_turn_uq"
        )
        assert rejected.returncode != 0
        assert "0009_rollout_seg_ordinal_uq 无法回滚" in rejected.stderr
        assert "UniqueViolation" not in rejected.stderr, "守卫必须先于约束重建给出可读报错"
        assert _MIGRATION_ACTIVE_INDEX in await _index_names(factory)

        # 清掉 deleted 行后回滚成功：不留下半完成状态
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "DELETE FROM conversation.conversation_rollout_segments "
                        "WHERE status = 'deleted'"
                    )
                )
        downgraded = await asyncio.to_thread(
            _alembic, temp_url, "downgrade", "0008_rollout_segment_turn_uq"
        )
        assert downgraded.returncode == 0, downgraded.stderr
        assert _MIGRATION_ACTIVE_INDEX not in await _index_names(factory)
        assert _MIGRATION_LEGACY_CONSTRAINT in await _constraint_names(factory)
    finally:
        await engine.dispose()
