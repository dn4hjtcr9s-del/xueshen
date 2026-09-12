"""Rollout 封存 → 指针 → 读取 → 删除 全链路集成测试（memory-rebuild §5.4 Phase 2）。

这是 Phase 2 的核心验收：真实 PostgreSQL + 真实对象存储（Local 目录模拟），
逐条对应 §5.4「Phase 2 验收」：

- 写入、封存、查询、读回的 sha256 / byte range / ordinal / content_hash 全部一致；
- 指针越界或 ordinal 错位时**拒绝返回**（宁可回退也不返回别的消息正文）；
- 对象被篡改时 reader 拒绝使用（sha256 复核）；
- 删除 thread：段对象物理删除 + manifest 落 tombstone；
- 空 turn 不产生 manifest 行、不产生对象。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.graph.state import SystemClock, SystemIdGenerator
from backend.conversation.persistence import rollout_manifests as manifests_repo
from backend.conversation.rollout.object_store import (
    FakeRolloutObjectStore,
    LocalRolloutObjectStore,
)
from backend.conversation.rollout.reader import RolloutReader
from backend.conversation.rollout.recorder import RolloutRecorder
from backend.conversation.rollout.sealer import RolloutSegmentSealer
from backend.conversation.services.thread_deletion import execute_delete_thread

pytest_plugins = ("tests.conversation.conftest",)

_NOW = datetime(2026, 9, 10, 5, 0, 0, tzinfo=UTC)


async def _seed_thread_and_turn(
    factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """插入 thread + 处于 running 的 turn + 两条消息，返回它们的 id。"""
    thread_id, user_id = uuid.uuid4(), uuid.uuid4()
    turn_id, user_message_id, assistant_message_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
                    ":user_message_id, 'running', 'worker-1', 3, :now, 1)"
                ),
                {
                    "turn_id": turn_id,
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "user_message_id": user_message_id,
                    "now": _NOW,
                },
            )
            for message_id, sequence, role, content in (
                (user_message_id, 1, "user", "椭圆的第一定义是什么？"),
                (assistant_message_id, 2, "assistant", "椭圆是到两定点距离之和为常数的点的轨迹。"),
            ):
                await session.execute(
                    text(
                        "INSERT INTO conversation.conversation_messages ("
                        "message_id, thread_id, turn_id, user_id, sequence, role, content, "
                        "status, content_hash, occurred_at"
                        ") VALUES ("
                        ":message_id, :thread_id, :turn_id, :user_id, :sequence, :role, :content, "
                        "'completed', :content_hash, :now)"
                    ),
                    {
                        "message_id": message_id,
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "user_id": user_id,
                        "sequence": sequence,
                        "role": role,
                        "content": content,
                        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                        "now": _NOW,
                    },
                )
    return {
        "thread_id": thread_id,
        "user_id": user_id,
        "turn_id": turn_id,
        "user_message_id": user_message_id,
        "assistant_message_id": assistant_message_id,
        "user_content": "椭圆的第一定义是什么？",
        "assistant_content": "椭圆是到两定点距离之和为常数的点的轨迹。",
    }


def _build(
    factory: async_sessionmaker[AsyncSession], root: Path, object_store: Any
) -> RolloutRecorder:
    return RolloutRecorder(
        root=root,
        clock=SystemClock(),
        id_generator=SystemIdGenerator(),
        logger=logging.getLogger("test.rollout.e2e"),
        sealer=RolloutSegmentSealer(
            session_factory=factory,
            object_store=object_store,
            logger=logging.getLogger("test.rollout.e2e"),
        ),
    )


async def _record_turn(recorder: RolloutRecorder, ids: dict[str, Any]) -> None:
    """写入一个 turn 的两条消息并关闭（触发封存）。"""
    await recorder.open_turn(
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        user_id=ids["user_id"],
        thread_created_at=_NOW,
        fence=("worker-1", 3),
    )
    await recorder.record(
        "user_message",
        payload={
            "message_id": str(ids["user_message_id"]),
            "sequence": 1,
            "role": "user",
            "content": ids["user_content"],
            "content_hash": hashlib.sha256(ids["user_content"].encode()).hexdigest(),
            "occurred_at": _NOW.isoformat(),
        },
    )
    await recorder.record(
        "assistant_message",
        payload={
            "message_id": str(ids["assistant_message_id"]),
            "sequence": 2,
            "role": "assistant",
            "content": ids["assistant_content"],
            "content_hash": hashlib.sha256(ids["assistant_content"].encode()).hexdigest(),
            "occurred_at": _NOW.isoformat(),
        },
    )
    await recorder.close_turn()


async def _load_message(
    factory: async_sessionmaker[AsyncSession], message_id: uuid.UUID
) -> dict[str, Any]:
    async with factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT * FROM conversation.conversation_messages "
                        "WHERE message_id = :message_id"
                    ),
                    {"message_id": message_id},
                )
            )
            .mappings()
            .one()
        )
    return dict(row)


# ---------------------------------------------------------------------------
# 封存
# ---------------------------------------------------------------------------


async def test_seal_writes_manifest_pointers_and_object(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """§5.4 验收：sha256 / byte range / ordinal / content_hash 全链路一致。"""
    factory = conversation_session_factory
    ids = await _seed_thread_and_turn(factory)
    object_store = LocalRolloutObjectStore(root=tmp_path)
    recorder = _build(factory, tmp_path, object_store)

    await _record_turn(recorder, ids)
    await recorder.aclose()

    async with factory() as session:
        segments = await manifests_repo.list_by_thread(session, ids["thread_id"])
    assert len(segments) == 1
    segment = segments[0]
    assert segment["status"] == "sealed"
    assert segment["ordinal_start"] == 0
    assert segment["ordinal_end"] == 2, "thread_meta + 两条消息"
    assert segment["object_key"] and segment["object_etag"] and segment["sha256"]
    assert segment["sealed_at"] is not None

    # 对象内容与 manifest 记录的 sha256 / 字节数一致
    data = await object_store.get(key=str(segment["object_key"]))
    assert hashlib.sha256(data).hexdigest() == segment["sha256"]
    assert len(data) == segment["byte_size"]

    # 指针：字节范围精确指向该消息那一行
    user_row = await _load_message(factory, ids["user_message_id"])
    assert user_row["segment_id"] == segment["segment_id"]
    assert user_row["rollout_ordinal"] == 1
    snippet = data[user_row["rollout_byte_offset_start"] : user_row["rollout_byte_offset_end"]]
    assert snippet.endswith(b"\n")
    assert snippet.count(b"\n") == 1, "指针区间必须恰好是一行"
    assert ids["user_content"].encode() in snippet


async def test_empty_turn_leaves_no_manifest_and_no_object(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """空 turn：既不建段文件，也不建 manifest 行，更不产生对象。"""
    factory = conversation_session_factory
    ids = await _seed_thread_and_turn(factory)
    object_store = LocalRolloutObjectStore(root=tmp_path)
    recorder = _build(factory, tmp_path, object_store)

    await recorder.open_turn(
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        user_id=ids["user_id"],
        thread_created_at=_NOW,
        fence=("worker-1", 3),
    )
    await recorder.close_turn()
    await recorder.aclose()

    async with factory() as session:
        assert await manifests_repo.list_by_thread(session, ids["thread_id"]) == []
    assert await object_store.list_prefix(prefix="rollouts/") == []


async def test_seal_requires_fence_owner(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """fencing：lease_generation 不匹配的 worker 不得封存（失租者不写 manifest）。"""
    factory = conversation_session_factory
    ids = await _seed_thread_and_turn(factory)
    object_store = LocalRolloutObjectStore(root=tmp_path)
    recorder = _build(factory, tmp_path, object_store)

    await recorder.open_turn(
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        user_id=ids["user_id"],
        thread_created_at=_NOW,
        fence=("worker-1", 99),  # 生成号不符
    )
    await recorder.record(
        "user_message",
        payload={
            "message_id": str(ids["user_message_id"]),
            "sequence": 1,
            "role": "user",
            "content": ids["user_content"],
            "content_hash": hashlib.sha256(ids["user_content"].encode()).hexdigest(),
            "occurred_at": _NOW.isoformat(),
        },
    )
    await recorder.close_turn()
    await recorder.aclose()

    async with factory() as session:
        segments = await manifests_repo.list_by_thread(session, ids["thread_id"])
    assert len(segments) == 1
    assert segments[0]["status"] == "open", "fencing 不通过时不得转 sealed"
    # 对象已上传（顺序铁律：先上传再写 manifest），成为孤儿交给 reconcile
    assert len(await object_store.list_prefix(prefix="rollouts/")) == 1


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


async def test_reader_returns_exact_message_content(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    factory = conversation_session_factory
    ids = await _seed_thread_and_turn(factory)
    object_store = LocalRolloutObjectStore(root=tmp_path)
    recorder = _build(factory, tmp_path, object_store)
    await _record_turn(recorder, ids)
    await recorder.aclose()

    reader = RolloutReader(
        session_factory=factory, object_store=object_store, rollout_root=tmp_path
    )
    for message_id, expected in (
        (ids["user_message_id"], ids["user_content"]),
        (ids["assistant_message_id"], ids["assistant_content"]),
    ):
        row = await _load_message(factory, message_id)
        assert await reader.read_message_content(message_row=row) == expected


async def test_reader_falls_back_when_object_is_missing(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """对象缺失 → reader 返回 None，由调用方回退 DB 正文（不抛错、不返回坏数据）。"""
    factory = conversation_session_factory
    ids = await _seed_thread_and_turn(factory)
    object_store = FakeRolloutObjectStore()
    recorder = _build(factory, tmp_path, object_store)
    await _record_turn(recorder, ids)
    await recorder.aclose()

    for key in object_store.keys():
        object_store.drop(key)
    # 换一个空的本地根目录模拟"另一个节点"：本用例要验证的是对象缺失时**不能**
    # 返回坏数据，而不是本地热缓存命中（后者命中是设计内的正确行为）。
    reader = RolloutReader(
        session_factory=factory,
        object_store=object_store,
        rollout_root=tmp_path / "another-node",
    )
    row = await _load_message(factory, ids["user_message_id"])
    assert await reader.read_message_content(message_row=row) is None


async def test_reader_rejects_tampered_object(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """对象被篡改 → sha256 复核失败 → 拒绝使用（宁可回退也不返回可疑正文）。"""
    factory = conversation_session_factory
    ids = await _seed_thread_and_turn(factory)
    object_store = FakeRolloutObjectStore()
    recorder = _build(factory, tmp_path, object_store)
    await _record_turn(recorder, ids)
    await recorder.aclose()

    for key in object_store.keys():
        object_store.corrupt(key)
    # 同上：换节点视角，强制走对象存储分支才能验证 sha256 复核
    reader = RolloutReader(
        session_factory=factory,
        object_store=object_store,
        rollout_root=tmp_path / "another-node",
    )
    row = await _load_message(factory, ids["user_message_id"])
    assert await reader.read_message_content(message_row=row) is None


async def test_reader_rejects_pointer_with_wrong_ordinal(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """指针区间合法但 ordinal 对不上时必须拒绝——否则会静默返回**别的消息**正文。"""
    factory = conversation_session_factory
    ids = await _seed_thread_and_turn(factory)
    object_store = LocalRolloutObjectStore(root=tmp_path)
    recorder = _build(factory, tmp_path, object_store)
    await _record_turn(recorder, ids)
    await recorder.aclose()

    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE conversation.conversation_messages "
                    "SET rollout_ordinal = 99 WHERE message_id = :message_id"
                ),
                {"message_id": ids["user_message_id"]},
            )
    reader = RolloutReader(
        session_factory=factory, object_store=object_store, rollout_root=tmp_path
    )
    row = await _load_message(factory, ids["user_message_id"])
    assert await reader.read_message_content(message_row=row) is None


async def test_message_without_pointer_returns_none(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """迁移期旧消息没有指针 → 一律回退 DB 正文。"""
    factory = conversation_session_factory
    ids = await _seed_thread_and_turn(factory)
    reader = RolloutReader(
        session_factory=factory,
        object_store=LocalRolloutObjectStore(root=tmp_path),
        rollout_root=tmp_path,
    )
    row = await _load_message(factory, ids["user_message_id"])
    assert row["segment_id"] is None
    assert await reader.read_message_content(message_row=row) is None


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


async def test_thread_deletion_removes_objects_and_tombstones_manifests(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """§1.8：删除 thread → 段对象物理删除 + manifest 落 tombstone。"""
    factory = conversation_session_factory
    ids = await _seed_thread_and_turn(factory)
    object_store = LocalRolloutObjectStore(root=tmp_path)
    recorder = _build(factory, tmp_path, object_store)
    await _record_turn(recorder, ids)
    await recorder.aclose()
    assert len(await object_store.list_prefix(prefix="rollouts/")) == 1

    job_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_jobs ("
                    "job_id, job_type, thread_id, user_id, status, attempt_count, next_attempt_at"
                    ") VALUES ("
                    ":job_id, 'delete_thread', :thread_id, :user_id, 'processing', 0, :now)"
                ),
                {
                    "job_id": job_id,
                    "thread_id": ids["thread_id"],
                    "user_id": ids["user_id"],
                    "now": _NOW,
                },
            )
            await session.execute(
                text(
                    "UPDATE conversation.conversation_turns "
                    "SET status = 'completed' WHERE thread_id = :thread_id"
                ),
                {"thread_id": ids["thread_id"]},
            )
    async with factory() as session:
        async with session.begin():
            result = await execute_delete_thread(
                session,
                job_id=job_id,
                thread_id=ids["thread_id"],
                deletion_generation=0,
                worker_id="worker-1",
                object_store=object_store,
            )
    assert result == "done"
    assert await object_store.list_prefix(prefix="rollouts/") == [], "对象应被物理删除"

    async with factory() as session:
        segments = await manifests_repo.list_by_thread(
            session, ids["thread_id"], include_deleted=True
        )
    assert len(segments) == 1
    assert segments[0]["status"] == "deleted"
    assert segments[0]["deleted_at"] is not None


async def test_reader_refuses_tombstoned_segment_even_if_object_exists(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """软删（tombstone）后 reader 必须拒绝回读——即使对象还在。

    §5.4 验收："删除后的 message 永不从 rollout 回读"。若只依赖对象被物理删除，
    在"已 tombstone 但对象尚未删除"的窗口里就会把已删数据当正文返回。

    ``mark_deleted`` 现在同时清空四列指针（软删不触发 0007 的清指针触发器，留着就是
    指向已废段的悬垂引用），因此本用例的两道防线都要验：指针已被清空，且即便手工
    把指针写回去，reader 也会按 ``status='deleted'`` 拒绝。
    """
    factory = conversation_session_factory
    ids = await _seed_thread_and_turn(factory)
    object_store = LocalRolloutObjectStore(root=tmp_path)
    recorder = _build(factory, tmp_path, object_store)
    await _record_turn(recorder, ids)
    await recorder.aclose()

    async with factory() as session:
        async with session.begin():
            segments = await manifests_repo.list_by_thread(session, ids["thread_id"])
            await manifests_repo.mark_deleted(session, segment_id=segments[0]["segment_id"])

    reader = RolloutReader(
        session_factory=factory, object_store=object_store, rollout_root=tmp_path
    )
    row = await _load_message(factory, ids["user_message_id"])
    assert row["segment_id"] is None, "软删必须同时清空指针（不留悬垂引用）"
    assert len(await object_store.list_prefix(prefix="rollouts/")) == 1
    assert await reader.read_message_content(message_row=row) is None

    # 第二道防线：把指针手工写回已删段，reader 仍必须按 status 拒绝
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE conversation.conversation_messages "
                    "SET segment_id = :segment_id, rollout_ordinal = :ordinal, "
                    "    rollout_byte_offset_start = :start, rollout_byte_offset_end = :end "
                    "WHERE message_id = :message_id"
                ),
                {
                    "segment_id": segments[0]["segment_id"],
                    "ordinal": 1,
                    "start": 0,
                    "end": 1,
                    "message_id": ids["user_message_id"],
                },
            )
    restored = await _load_message(factory, ids["user_message_id"])
    assert restored["segment_id"] is not None
    assert await reader.read_message_content(message_row=restored) is None
