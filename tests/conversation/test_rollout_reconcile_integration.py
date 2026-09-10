"""Rollout reconcile 真实 PostgreSQL 集成测试（memory-rebuild §5.4 Phase 2 / OPEN-005）。

补上 `reconcile.py` 的仓库内集成测试：此前的单测用假会话工厂按 SQL 文本分发，
**JOIN / WHERE 写错它发现不了**。这里用真实 conversation_test 库造出五类不一致，
验证扫描确实能检出、且 repair 只做被允许的两类修复。

五类（对应 reconcile.KIND_*）：
1. sealed 段的对象不存在；
2. sealed 段的对象存在但 sha256 与 manifest 不符（被篡改）；
3. open 段但本地热段文件不存在（换节点遗留的孤儿登记）；
4. 对象存储里有对象但没有任何 manifest 引用（已上传未登记）；
5. conversation_messages 的 rollout 指针越界 / ordinal 错位。
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.persistence import rollout_manifests as manifests_repo
from backend.conversation.rollout.object_store import (
    ROLLOUT_OBJECT_PREFIX,
    LocalRolloutObjectStore,
    build_object_key,
)
from backend.conversation.rollout.reconcile import (
    KIND_OPEN_LOCAL_MISSING,
    KIND_ORPHAN_OBJECT,
    KIND_POINTER_OUT_OF_RANGE,
    KIND_SEALED_CHECKSUM_MISMATCH,
    KIND_SEALED_OBJECT_MISSING,
    RolloutReconciler,
)

pytest_plugins = ("tests.conversation.conftest",)

_NOW = datetime(2026, 9, 10, 5, 0, 0, tzinfo=UTC)


async def _seed_thread(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    thread_id, user_id, turn_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
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
                    "user_message_id, status, next_attempt_at, expected_thread_version"
                    ") VALUES ("
                    ":turn_id, :thread_id, :user_id, :client_request_id, 'r', 'run', "
                    ":turn_id, 'completed', :now, 1)"
                ),
                {
                    "turn_id": turn_id,
                    "thread_id": thread_id,
                    "user_id": user_id,
                    "client_request_id": str(turn_id),
                    "now": _NOW,
                },
            )
    return {"thread_id": thread_id, "user_id": user_id, "turn_id": turn_id}


async def _new_turn(factory: async_sessionmaker[AsyncSession], ids: dict[str, Any]) -> uuid.UUID:
    """为同一 thread 再造一个 turn。

    ``uq_rollout_segment_turn_active`` 保证一个 turn 只能有一个非 deleted 段，
    因此"同一 thread 多个段"的用例必须用不同 turn——这正是该约束在起作用。
    """
    turn_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_turns ("
                    "turn_id, thread_id, user_id, client_request_id, request_id, run_id, "
                    "user_message_id, status, next_attempt_at, expected_thread_version"
                    ") VALUES ("
                    ":turn_id, :thread_id, :user_id, :client_request_id, 'r', 'run', "
                    ":turn_id, 'completed', :now, 1)"
                ),
                {
                    "turn_id": turn_id,
                    "thread_id": ids["thread_id"],
                    "user_id": ids["user_id"],
                    # 表上有 UNIQUE(thread_id, client_request_id)，同 thread 多 turn 必须换值
                    "client_request_id": str(turn_id),
                    "now": _NOW,
                },
            )
    return turn_id


async def _insert_segment(
    factory: async_sessionmaker[AsyncSession],
    *,
    thread_id: uuid.UUID,
    turn_id: uuid.UUID,
    status: str,
    ordinal_start: int = 0,
    ordinal_end: int | None = None,
    object_key: str | None = None,
    sha256: str | None = None,
    byte_size: int | None = None,
) -> dict[str, Any]:
    segment_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_rollout_segments ("
                    "segment_id, thread_id, turn_id, ordinal_start, ordinal_end, object_key, "
                    "object_etag, sha256, byte_size, status, created_at, sealed_at"
                    ") VALUES ("
                    ":segment_id, :thread_id, :turn_id, :ordinal_start, :ordinal_end, "
                    ":object_key, :object_etag, :sha256, :byte_size, :status, :now, :sealed_at)"
                ),
                {
                    "segment_id": segment_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "ordinal_start": ordinal_start,
                    "ordinal_end": ordinal_end,
                    "object_key": object_key,
                    "object_etag": "etag" if status == "sealed" else None,
                    "sha256": sha256,
                    "byte_size": byte_size,
                    "status": status,
                    "now": _NOW,
                    "sealed_at": _NOW if status == "sealed" else None,
                },
            )
    return {"segment_id": segment_id}


async def _insert_message_with_pointer(
    factory: async_sessionmaker[AsyncSession],
    ids: dict[str, Any],
    *,
    segment_id: uuid.UUID | None,
    ordinal: int,
    offset_start: int,
    offset_end: int,
) -> uuid.UUID:
    message_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_messages ("
                    "message_id, thread_id, turn_id, user_id, sequence, role, content, "
                    "status, content_hash, occurred_at, segment_id, rollout_ordinal, "
                    "rollout_byte_offset_start, rollout_byte_offset_end"
                    ") VALUES ("
                    ":message_id, :thread_id, :turn_id, :user_id, 1, 'user', '正文', "
                    "'completed', :content_hash, :now, :segment_id, :ordinal, :start, :end)"
                ),
                {
                    "message_id": message_id,
                    "thread_id": ids["thread_id"],
                    "turn_id": ids["turn_id"],
                    "user_id": ids["user_id"],
                    "content_hash": "a" * 64,
                    "now": _NOW,
                    "segment_id": segment_id,
                    "ordinal": ordinal,
                    "start": offset_start,
                    "end": offset_end,
                },
            )
    return message_id


def _key_for(thread_id: uuid.UUID, ordinal_start: int = 0) -> str:
    return build_object_key(
        thread_created_at=_NOW,
        thread_id=thread_id,
        ordinal_start=ordinal_start,
        segment_id=uuid.uuid4(),
    )


def _reconciler(
    factory: async_sessionmaker[AsyncSession], tmp_path: Path, store: Any
) -> RolloutReconciler:
    return RolloutReconciler(session_factory=factory, object_store=store, rollout_root=tmp_path)


# ---------------------------------------------------------------------------
# 五类 finding
# ---------------------------------------------------------------------------


async def test_scan_detects_sealed_object_missing(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    factory = conversation_session_factory
    ids = await _seed_thread(factory)
    store = LocalRolloutObjectStore(root=tmp_path)
    await _insert_segment(
        factory,
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        status="sealed",
        ordinal_end=2,
        object_key=_key_for(ids["thread_id"]),
        sha256="b" * 64,
        byte_size=10,
    )
    report = await _reconciler(factory, tmp_path, store).scan()
    kinds = [f.kind for f in report.findings]
    assert KIND_SEALED_OBJECT_MISSING in kinds
    assert report.ok is False


async def test_scan_detects_sealed_checksum_mismatch(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    factory = conversation_session_factory
    ids = await _seed_thread(factory)
    store = LocalRolloutObjectStore(root=tmp_path)
    key = _key_for(ids["thread_id"])
    await store.put_immutable(key=key, data=b'{"ordinal": 0}\n')
    # manifest 记一个与对象内容不同的 sha256 → 篡改
    await _insert_segment(
        factory,
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        status="sealed",
        ordinal_end=0,
        object_key=key,
        sha256="c" * 64,
        byte_size=999,
    )
    report = await _reconciler(factory, tmp_path, store).scan()
    assert KIND_SEALED_CHECKSUM_MISMATCH in [f.kind for f in report.findings]


async def test_scan_detects_open_local_missing(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """open 段但本地文件不存在（换节点后的孤儿登记）。"""
    factory = conversation_session_factory
    ids = await _seed_thread(factory)
    store = LocalRolloutObjectStore(root=tmp_path)
    await _insert_segment(
        factory,
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        status="open",
        ordinal_start=0,
    )
    report = await _reconciler(factory, tmp_path, store).scan()
    assert KIND_OPEN_LOCAL_MISSING in [f.kind for f in report.findings]


async def test_scan_detects_orphan_object(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """对象存在但没有任何 manifest 行引用它（已上传未登记）。"""
    factory = conversation_session_factory
    store = LocalRolloutObjectStore(root=tmp_path)
    await store.put_immutable(key=_key_for(uuid.uuid4()), data=b'{"ordinal": 0}\n')
    report = await _reconciler(factory, tmp_path, store).scan()
    assert KIND_ORPHAN_OBJECT in [f.kind for f in report.findings]


async def test_scan_detects_pointer_out_of_range(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """指针字节范围超出对象大小。"""
    factory = conversation_session_factory
    ids = await _seed_thread(factory)
    store = LocalRolloutObjectStore(root=tmp_path)
    data = b'{"ordinal": 0, "type": "thread_meta", "turn_id": null, "payload": {}}\n'
    key = _key_for(ids["thread_id"])
    await store.put_immutable(key=key, data=data)
    segment = await _insert_segment(
        factory,
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        status="sealed",
        ordinal_end=0,
        object_key=key,
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
    )
    await _insert_message_with_pointer(
        factory,
        ids,
        segment_id=segment["segment_id"],
        ordinal=0,
        offset_start=0,
        offset_end=len(data) + 500,  # 越界
    )
    report = await _reconciler(factory, tmp_path, store).scan()
    assert KIND_POINTER_OUT_OF_RANGE in [f.kind for f in report.findings]


async def test_scan_detects_pointer_ordinal_out_of_segment_range(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """指针 ordinal 不在段的 [ordinal_start, ordinal_end] 区间内。"""
    factory = conversation_session_factory
    ids = await _seed_thread(factory)
    store = LocalRolloutObjectStore(root=tmp_path)
    data = b'{"ordinal": 0, "type": "thread_meta", "turn_id": null, "payload": {}}\n'
    key = _key_for(ids["thread_id"])
    await store.put_immutable(key=key, data=data)
    segment = await _insert_segment(
        factory,
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        status="sealed",
        ordinal_end=1,
        object_key=key,
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
    )
    await _insert_message_with_pointer(
        factory,
        ids,
        segment_id=segment["segment_id"],
        ordinal=77,  # 远超出 [0, 1]
        offset_start=0,
        offset_end=len(data),
    )
    report = await _reconciler(factory, tmp_path, store).scan()
    assert KIND_POINTER_OUT_OF_RANGE in [f.kind for f in report.findings]


# ---------------------------------------------------------------------------
# 负例与 repair
# ---------------------------------------------------------------------------


async def test_scan_clean_state_reports_ok(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """一致的 sealed 段 + 正确指针 → 无 finding（避免误报）。"""
    factory = conversation_session_factory
    ids = await _seed_thread(factory)
    store = LocalRolloutObjectStore(root=tmp_path)
    data = b'{"ordinal": 0, "type": "thread_meta", "turn_id": null, "payload": {}}\n'
    key = _key_for(ids["thread_id"])
    await store.put_immutable(key=key, data=data)
    segment = await _insert_segment(
        factory,
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        status="sealed",
        ordinal_end=0,
        object_key=key,
        sha256=hashlib.sha256(data).hexdigest(),
        byte_size=len(data),
    )
    await _insert_message_with_pointer(
        factory,
        ids,
        segment_id=segment["segment_id"],
        ordinal=0,
        offset_start=0,
        offset_end=len(data),
    )
    report = await _reconciler(factory, tmp_path, store).scan()
    assert report.ok is True, report.summary
    assert report.findings == []


async def test_repair_dry_run_has_no_side_effects(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    factory = conversation_session_factory
    ids = await _seed_thread(factory)
    store = LocalRolloutObjectStore(root=tmp_path)
    await _insert_segment(
        factory,
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        status="sealed",
        ordinal_end=2,
        object_key=_key_for(ids["thread_id"]),
        sha256="d" * 64,
        byte_size=10,
    )
    reconciler = _reconciler(factory, tmp_path, store)
    report = await reconciler.scan()
    actions = await reconciler.repair(report, dry_run=True)
    assert actions, "dry-run 也应给出将执行的动作说明"

    async with factory() as session:
        rows = await manifests_repo.list_by_thread(session, ids["thread_id"])
    assert rows[0]["status"] == "sealed", "dry-run 不得改动任何状态"


async def test_repair_marks_only_allowed_kinds_and_keeps_objects(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """repair 只标"对象缺失的 sealed"与"本地缺失的 open"，不删对象、不标篡改段。"""
    factory = conversation_session_factory
    ids = await _seed_thread(factory)
    store = LocalRolloutObjectStore(root=tmp_path)

    # ① 可修：sealed 但对象缺失
    missing = await _insert_segment(
        factory,
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        status="sealed",
        ordinal_start=0,
        ordinal_end=1,
        object_key=_key_for(ids["thread_id"], ordinal_start=0),
        sha256="e" * 64,
        byte_size=5,
    )
    # ② 不可修：sealed 且被篡改（对象在，只是哈希不符）
    tamper_key = _key_for(ids["thread_id"], ordinal_start=10)
    await store.put_immutable(key=tamper_key, data=b'{"ordinal": 10}\n')
    tampered = await _insert_segment(
        factory,
        thread_id=ids["thread_id"],
        turn_id=await _new_turn(factory, ids),
        status="sealed",
        ordinal_start=10,
        ordinal_end=10,
        object_key=tamper_key,
        sha256="f" * 64,
        byte_size=17,
    )
    reconciler = _reconciler(factory, tmp_path, store)
    report = await reconciler.scan()
    await reconciler.repair(report, dry_run=False)

    async with factory() as session:
        missing_row = await manifests_repo.get_by_id(session, missing["segment_id"])
        tampered_row = await manifests_repo.get_by_id(session, tampered["segment_id"])
    assert missing_row is not None and missing_row["status"] == "deleted"
    assert tampered_row is not None and tampered_row["status"] == "sealed"
    # 对象一个都不能少：repair 不负责删对象
    assert tamper_key in [stat.key for stat in await store.list_prefix(prefix="rollouts/")]


async def test_scan_respects_limit(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    factory = conversation_session_factory
    ids = await _seed_thread(factory)
    store = LocalRolloutObjectStore(root=tmp_path)
    for ordinal in range(3):
        await _insert_segment(
            factory,
            thread_id=ids["thread_id"],
            turn_id=await _new_turn(factory, ids),
            status="sealed",
            ordinal_start=ordinal,
            ordinal_end=ordinal,
            object_key=_key_for(ids["thread_id"], ordinal_start=ordinal),
            sha256="1" * 64,
            byte_size=1,
        )
    report = await _reconciler(factory, tmp_path, store).scan(limit=2)
    assert len(report.of_kind(KIND_SEALED_OBJECT_MISSING)) <= 2


async def test_scan_rejects_non_positive_limit(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    store = LocalRolloutObjectStore(root=tmp_path)
    reconciler = _reconciler(conversation_session_factory, tmp_path, store)
    try:
        await reconciler.scan(limit=0)
    except ValueError as exc:
        assert "limit" in str(exc)
    else:  # pragma: no cover - 明确失败而非静默通过
        raise AssertionError("limit=0 应当报错")


async def test_scan_sees_objects_under_rollouts_prefix_only(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """非 rollouts/ 前缀的对象不算孤儿（避免把别人的 bucket 内容误报）。"""
    factory = conversation_session_factory
    store = LocalRolloutObjectStore(root=tmp_path)
    await store.put_immutable(key="community/images/a.png", data=b"png")
    report = await _reconciler(factory, tmp_path, store).scan()
    assert report.of_kind(KIND_ORPHAN_OBJECT) == []


def test_rollout_prefix_constant() -> None:
    assert ROLLOUT_OBJECT_PREFIX == "rollouts"
