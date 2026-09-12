"""thread 删除 / retention 必须同时删除本地热缓存段（评审 C-3 / I-6）。

真实 PostgreSQL（conversation_test）+ 真实文件系统，覆盖三件事：

1. 删除 thread 后 **objects 镜像与 ``{root}/threads/...`` 热缓存段都不存在**，
   manifest 已落 tombstone，且遍历整个 rollout 根目录找不到任何用户原文；
2. **flag 关闭时同样删干净**（I-6：对象存储构造脱离 ``CONVERSATION_ROLLOUT_ENABLED``），
   否则 enable → disable 之后再删 thread，对象会永久不可达；
3. 缺少对象存储时**拒绝落 tombstone**、Job 保持可重试（不允许"没删数据却宣称删了"）；
4. CLI 的 ``delete-thread-rollouts`` 与 ``retention-scan`` 两条运维路径同样删热段。

参考 tests/conversation/test_rollout_seal_read.py 的段构造方式（真实 recorder 封存）。

运行：
    DATABASE_URL=postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test \\
    CONVERSATION_DATABASE_URL=...@127.0.0.1:55432/conversation_test \\
    uv run pytest tests/integration/test_rollout_hot_cache_deletion.py -q
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.cli import rollout as rollout_cli
from backend.conversation.graph.state import SystemClock, SystemIdGenerator
from backend.conversation.persistence import rollout_manifests as manifests_repo
from backend.conversation.rollout.object_store import (
    LocalRolloutObjectStore,
    local_path_for_object_key,
)
from backend.conversation.rollout.recorder import RolloutRecorder
from backend.conversation.rollout.sealer import RolloutSegmentSealer
from backend.conversation.services.thread_deletion import execute_delete_thread
from backend.conversation.worker.main import build_rollout_runtime
from backend.settings import Settings

pytest_plugins = ("tests.conversation.conftest",)

_NOW = datetime(2026, 9, 10, 5, 0, 0, tzinfo=UTC)
_USER_TEXT = "抛物线的焦点坐标怎么求？"
_ASSISTANT_TEXT = "抛物线 y^2=2px 的焦点是 (p/2, 0)。"
_LOGGER = logging.getLogger("test.rollout.hot_cache")


# ---------------------------------------------------------------------------
# 数据构造
# ---------------------------------------------------------------------------


async def _seed_thread_and_turn(factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """插入 thread + running turn + 两条消息（正文即用户原文）。"""
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
                (user_message_id, 1, "user", _USER_TEXT),
                (assistant_message_id, 2, "assistant", _ASSISTANT_TEXT),
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
    }


def _build_recorder(
    factory: async_sessionmaker[AsyncSession], root: Path, object_store: Any
) -> RolloutRecorder:
    return RolloutRecorder(
        root=root,
        clock=SystemClock(),
        id_generator=SystemIdGenerator(),
        logger=_LOGGER,
        sealer=RolloutSegmentSealer(
            session_factory=factory,
            object_store=object_store,
            logger=_LOGGER,
        ),
    )


async def _record_turn(recorder: RolloutRecorder, ids: dict[str, Any]) -> None:
    """写入一个 turn 的两条消息并关闭（触发封存：热文件 + 对象 + manifest）。"""
    await recorder.open_turn(
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        user_id=ids["user_id"],
        thread_created_at=_NOW,
        fence=("worker-1", 3),
    )
    for message_id, sequence, role, content in (
        (ids["user_message_id"], 1, "user", _USER_TEXT),
        (ids["assistant_message_id"], 2, "assistant", _ASSISTANT_TEXT),
    ):
        await recorder.record(
            f"{role}_message",
            payload={
                "message_id": str(message_id),
                "sequence": sequence,
                "role": role,
                "content": content,
                "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                "occurred_at": _NOW.isoformat(),
            },
        )
    await recorder.close_turn()


async def _sealed_segment(
    factory: async_sessionmaker[AsyncSession], root: Path, object_store: Any
) -> dict[str, Any]:
    """造一个已封存段，返回 ids + segment + 热缓存路径（断言前先证明两者都真实存在）。"""
    ids = await _seed_thread_and_turn(factory)
    recorder = _build_recorder(factory, root, object_store)
    await _record_turn(recorder, ids)
    await recorder.aclose()

    async with factory() as session:
        segments = await manifests_repo.list_by_thread(session, ids["thread_id"])
    assert len(segments) == 1, "封存应恰好产生一个段"
    segment = segments[0]
    assert segment["status"] == "sealed"
    ids["segment"] = segment
    object_key = str(segment["object_key"])
    ids["object_key"] = object_key
    ids["hot_path"] = local_path_for_object_key(root=root, object_key=object_key)
    assert ids["hot_path"].is_file(), "热缓存段必须真实存在，否则本用例证明不了 C-3"
    assert _USER_TEXT.encode() in ids["hot_path"].read_bytes()
    return ids


async def _prepare_delete_job(
    factory: async_sessionmaker[AsyncSession], ids: dict[str, Any]
) -> tuple[uuid.UUID, str]:
    """终止活动 Turn 并插入一个已 claim 的 delete_thread Job（返回 job_id, worker_id）。"""
    worker_id = "worker-1"
    job_id = uuid.uuid4()
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE conversation.conversation_turns SET status = 'completed' "
                    "WHERE thread_id = :thread_id"
                ),
                {"thread_id": ids["thread_id"]},
            )
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_jobs ("
                    "job_id, job_type, thread_id, user_id, status, attempt_count,"
                    " next_attempt_at, lease_owner, lease_generation, lease_expires_at"
                    ") VALUES ("
                    ":job_id, 'delete_thread', :thread_id, :user_id, 'processing', 1,"
                    " :now, :worker_id, 1, :now + interval '60 seconds')"
                ),
                {
                    "job_id": job_id,
                    "thread_id": ids["thread_id"],
                    "user_id": ids["user_id"],
                    "worker_id": worker_id,
                    "now": _NOW,
                },
            )
    return job_id, worker_id


async def _run_delete_thread(
    factory: async_sessionmaker[AsyncSession],
    ids: dict[str, Any],
    object_store: Any,
) -> tuple[str, uuid.UUID]:
    job_id, worker_id = await _prepare_delete_job(factory, ids)
    async with factory() as session:
        async with session.begin():
            result = await execute_delete_thread(
                session,
                job_id=job_id,
                thread_id=ids["thread_id"],
                deletion_generation=0,
                worker_id=worker_id,
                object_store=object_store,
            )
    return result, job_id


async def _manifest_rows(
    factory: async_sessionmaker[AsyncSession], thread_id: uuid.UUID
) -> list[dict[str, Any]]:
    async with factory() as session:
        return await manifests_repo.list_by_thread(session, thread_id, include_deleted=True)


def _files_containing_text(root: Path, *needles: str) -> list[str]:
    """遍历用户目录，返回仍含任意 needle 的文件（相对路径）。"""
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if any(needle.encode() in data for needle in needles):
            hits.append(str(path.relative_to(root)))
    return hits


def _assert_no_user_text_left(root: Path) -> None:
    """删除路径不得留下任何可读的用户原文（对象 + 热缓存 + 临时文件全覆盖）。"""
    assert _files_containing_text(root, _USER_TEXT, _ASSISTANT_TEXT) == [], (
        "删除后仍有文件残留用户原文"
    )


# ---------------------------------------------------------------------------
# thread 删除：flag 打开
# ---------------------------------------------------------------------------


async def test_thread_deletion_removes_hot_segment_and_object(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """C-3：删 thread 后对象镜像与热缓存段都消失，manifest 落 tombstone。"""
    factory = conversation_session_factory
    root = tmp_path / "rollout-root"
    settings = Settings(
        app_env="test",
        conversation_rollout_root=str(root),
        conversation_rollout_enabled=True,
    )
    store, recorder_assembly = build_rollout_runtime(
        settings=settings, session_factory=factory, logger=_LOGGER
    )
    assert recorder_assembly is not None, "flag 打开时必须装配 recorder"

    ids = await _sealed_segment(factory, root, store)
    assert len(await store.list_prefix(prefix="rollouts/")) == 1, "对象镜像应已存在"

    result, _job_id = await _run_delete_thread(factory, ids, store)
    assert result == "done"

    assert await store.list_prefix(prefix="rollouts/") == [], "对象镜像必须被物理删除"
    assert not ids["hot_path"].exists(), "热缓存段必须被物理删除（C-3 核心断言）"
    rows = await _manifest_rows(factory, ids["thread_id"])
    assert rows[0]["status"] == "deleted" and rows[0]["deleted_at"] is not None
    _assert_no_user_text_left(root)


# ---------------------------------------------------------------------------
# thread 删除：flag 关闭（I-6）
# ---------------------------------------------------------------------------


async def test_thread_deletion_with_rollout_flag_disabled_still_deletes(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """I-6：enable → disable 之后删 thread，对象与热文件同样必须消失。"""
    factory = conversation_session_factory
    root = tmp_path / "rollout-root"
    # 段是"当年 flag 还开着"时写下的
    ids = await _sealed_segment(factory, root, LocalRolloutObjectStore(root=root))

    # 删除时 flag 已关闭：对象存储仍必须由 worker 装配出来
    settings = Settings(
        app_env="test",
        conversation_rollout_root=str(root),
        conversation_rollout_enabled=False,
    )
    store, recorder_assembly = build_rollout_runtime(
        settings=settings, session_factory=factory, logger=_LOGGER
    )
    assert recorder_assembly is None, "flag 关闭时不得装配 recorder"

    result, _job_id = await _run_delete_thread(factory, ids, store)
    assert result == "done"

    assert await store.list_prefix(prefix="rollouts/") == []
    assert not ids["hot_path"].exists()
    rows = await _manifest_rows(factory, ids["thread_id"])
    assert rows[0]["status"] == "deleted"
    _assert_no_user_text_left(root)


async def test_missing_object_store_refuses_tombstone(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """I-6：缺对象存储时保持 deleting、Job 可重试，绝不落 tombstone。"""
    factory = conversation_session_factory
    root = tmp_path / "rollout-root"
    ids = await _sealed_segment(factory, root, LocalRolloutObjectStore(root=root))

    result, job_id = await _run_delete_thread(factory, ids, None)
    assert result == "wait", "没有对象存储时不得返回 done"

    rows = await _manifest_rows(factory, ids["thread_id"])
    assert rows[0]["status"] == "sealed", "不得落 tombstone"
    async with factory() as session:
        job = (
            (
                await session.execute(
                    text("SELECT * FROM conversation.conversation_jobs WHERE job_id = :job_id"),
                    {"job_id": job_id},
                )
            )
            .mappings()
            .one()
        )
        thread = (
            await session.execute(
                text(
                    "SELECT status FROM conversation.conversation_threads "
                    "WHERE thread_id = :thread_id"
                ),
                {"thread_id": ids["thread_id"]},
            )
        ).scalar_one()
    assert job["status"] == "retry_wait"
    assert job["last_error_code"] == "ROLLOUT_OBJECT_STORE_MISSING"
    assert job["attempt_count"] == 1, "等待不得递增 attempt_count（R3）"
    assert thread != "deleted", "未删数据就不得宣称 thread 已删除"
    # 数据确实还在（本用例证明的是"没删就不说谎"，不是"已经删了"）
    assert ids["hot_path"].is_file()


# ---------------------------------------------------------------------------
# CLI：delete-thread-rollouts / retention-scan
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_env(
    conversation_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """把 CLI 指向 conversation_test 库与临时 rollout 根目录（同 CLI 集成测试做法）。"""
    rollout_root = tmp_path / "rollout-root"
    rollout_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(
        "CONVERSATION_DATABASE_URL", str(conversation_settings.conversation_database_url)
    )
    monkeypatch.setenv("CONVERSATION_ROLLOUT_ROOT", str(rollout_root))
    monkeypatch.setenv("CONVERSATION_ROLLOUT_OBJECT_STORE", "local")
    from backend.settings import get_settings

    get_settings.cache_clear()
    yield rollout_root
    get_settings.cache_clear()


async def _run_cli(argv: list[str]) -> int:
    args = rollout_cli.build_parser().parse_args(argv)
    return await rollout_cli._invoke_handler(args.func, args)


async def test_cli_delete_thread_rollouts_removes_hot_segments(
    conversation_session_factory: async_sessionmaker[AsyncSession], cli_env: Path
) -> None:
    """C-3：CLI 删除路径同样必须删掉热缓存段，而不只是 objects 镜像。"""
    factory = conversation_session_factory
    store = LocalRolloutObjectStore(root=cli_env)
    ids = await _sealed_segment(factory, cli_env, store)

    assert (
        await _run_cli(["delete-thread-rollouts", "--thread-id", str(ids["thread_id"]), "--apply"])
        == 0
    )

    assert await store.list_prefix(prefix="rollouts/") == []
    assert not ids["hot_path"].exists(), "CLI 删除必须删热缓存段"
    rows = await _manifest_rows(factory, ids["thread_id"])
    assert rows[0]["status"] == "deleted"
    _assert_no_user_text_left(cli_env)


async def test_cli_retention_scan_removes_hot_segments(
    conversation_session_factory: async_sessionmaker[AsyncSession], cli_env: Path
) -> None:
    """C-3：retention 清理同样必须删掉热缓存段。"""
    factory = conversation_session_factory
    store = LocalRolloutObjectStore(root=cli_env)
    ids = await _sealed_segment(factory, cli_env, store)
    # 把段创建时间推到保留期外（retention 候选只认 manifest.created_at）
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "UPDATE conversation.conversation_rollout_segments "
                    "SET created_at = :past WHERE segment_id = :segment_id"
                ),
                {
                    "past": datetime.now(UTC) - timedelta(days=2),
                    "segment_id": ids["segment"]["segment_id"],
                },
            )

    assert await _run_cli(["retention-scan", "--older-than-days", "1", "--apply"]) == 0

    assert await store.list_prefix(prefix="rollouts/") == []
    assert not ids["hot_path"].exists(), "retention 必须删热缓存段"
    rows = await _manifest_rows(factory, ids["thread_id"])
    assert rows[0]["status"] == "deleted"
    _assert_no_user_text_left(cli_env)
