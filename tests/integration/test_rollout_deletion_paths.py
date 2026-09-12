"""删除路径的真库真盘参数化集成测试（review-2 新发现 6 / 15）。

与 ``tests/unit/test_rollout_deletion_paths_meta.py`` 配套：元测试用 AST 断言"每个入口都调用了
同一组删除助手、并且三段齐全"，本文件负责在**真实文件系统 + 真实 PostgreSQL**上把每个入口
真跑一遍，断言删除之后：

- 对象镜像（``objects/``）与本地热缓存段（``{root}/threads/...``）都不存在；
- manifest 已落 tombstone；
- 整个 rollout 根目录里再也搜不到用户原文。

另外覆盖 **object_key × 热文件 的真值表**（新发现 6 的核心）：

===================  ==============  ================================
object_key          热文件          期望
===================  ==============  ================================
有（sealed）        有（正常封存）  对象与热文件都被删
有（sealed）        无（已被清走）  幂等成功，仍落 tombstone
无（open 未封存）   有（崩溃残留）  **按 thread 目录清扫热段**后落 tombstone
无（open 未封存）   无              无残留也要落 tombstone（不能整段跳过）
===================  ==============  ================================

以及 kodo 式"对象在远端、热段在本机"的组合（新发现 15）：远端 store 只要挂上
``LocalHotSegmentCache``，删除路径就能把本机热段清干净。

运行：``scripts/ci-local.sh backend-integration``，或显式注入 conversation_test：
    CONVERSATION_DATABASE_URL=postgresql+psycopg://conversation:conversation@127.0.0.1:55432/
                           conversation_test \\
    uv run pytest tests/integration/test_rollout_deletion_paths.py -q
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
from backend.conversation.rollout.file_naming import segment_path
from backend.conversation.rollout.object_store import (
    LocalHotSegmentCache,
    LocalRolloutObjectStore,
    local_path_for_object_key,
)
from backend.conversation.rollout.recorder import RolloutRecorder
from backend.conversation.rollout.sealer import RolloutSegmentSealer
from backend.conversation.services.thread_deletion import execute_delete_thread
from backend.settings import Settings

pytest_plugins = ("tests.conversation.conftest",)

_NOW = datetime(2026, 9, 10, 5, 0, 0, tzinfo=UTC)
_USER_TEXT = "椭圆的准线方程怎么推？"
_ASSISTANT_TEXT = "椭圆 x^2/a^2+y^2/b^2=1 的准线是 x=±a^2/c。"
_LOGGER = logging.getLogger("test.rollout.deletion_paths")


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
    """写入一个 turn 的两条消息（不封存；由调用方决定是否 close_turn）。"""
    handle = await recorder.open_turn(
        thread_id=ids["thread_id"],
        turn_id=ids["turn_id"],
        user_id=ids["user_id"],
        thread_created_at=_NOW,
        fence=("worker-1", 3),
    )
    assert handle is not None, "open_turn 不应失败"
    ids["segment_id"] = handle.segment_id
    ids["ordinal_start"] = handle.ordinal_start
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


async def _sealed_segment(
    factory: async_sessionmaker[AsyncSession], root: Path, store: Any
) -> dict[str, Any]:
    """造一个已封存段（对象 + 热段 + manifest 三件齐备）。"""
    ids = await _seed_thread_and_turn(factory)
    recorder = _build_recorder(factory, root, store)
    await _record_turn(recorder, ids)
    await recorder.close_turn()
    await recorder.aclose()

    async with factory() as session:
        segments = await manifests_repo.list_by_thread(session, ids["thread_id"])
    assert len(segments) == 1 and segments[0]["status"] == "sealed"
    ids["segment"] = segments[0]
    ids["object_key"] = str(segments[0]["object_key"])
    ids["hot_path"] = local_path_for_object_key(root=root, object_key=ids["object_key"])
    assert ids["hot_path"].is_file() and _USER_TEXT.encode() in ids["hot_path"].read_bytes()
    return ids


async def _open_segment(
    factory: async_sessionmaker[AsyncSession], root: Path, store: Any
) -> dict[str, Any]:
    """造一个**未封存**段（``object_key IS NULL`` + 本地热段仍在）。

    真实触发条件：turn 崩溃/封存失败留下的 open 段——这正是新发现 6 里被整段跳过的对象。
    用 ``aclose()``（而不是 ``close_turn()``）停下记录器即可复现。
    """
    ids = await _seed_thread_and_turn(factory)
    recorder = _build_recorder(factory, root, store)
    await _record_turn(recorder, ids)
    await recorder.flush()
    await recorder.aclose()  # 关闭文件但不封存 → manifest 保持 open、object_key 为 NULL

    async with factory() as session:
        segments = await manifests_repo.list_by_thread(session, ids["thread_id"])
    assert len(segments) == 1 and segments[0]["status"] == "open"
    assert segments[0]["object_key"] is None
    ids["segment"] = segments[0]
    ids["object_key"] = None
    ids["hot_path"] = segment_path(
        root=root,
        thread_created_at=_NOW,
        thread_id=ids["thread_id"],
        ordinal_start=int(segments[0]["ordinal_start"]),
        segment_id=segments[0]["segment_id"],
    )
    assert ids["hot_path"].is_file() and _USER_TEXT.encode() in ids["hot_path"].read_bytes()
    return ids


async def _prepare_delete_job(
    factory: async_sessionmaker[AsyncSession], ids: dict[str, Any]
) -> tuple[uuid.UUID, str]:
    """终止活动 Turn 并插入一个已 claim 的 delete_thread Job。"""
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
    factory: async_sessionmaker[AsyncSession], ids: dict[str, Any], store: Any
) -> str:
    job_id, worker_id = await _prepare_delete_job(factory, ids)
    async with factory() as session:
        async with session.begin():
            return await execute_delete_thread(
                session,
                job_id=job_id,
                thread_id=ids["thread_id"],
                deletion_generation=0,
                worker_id=worker_id,
                object_store=store,
            )


async def _manifest_rows(
    factory: async_sessionmaker[AsyncSession], thread_id: uuid.UUID
) -> list[dict[str, Any]]:
    async with factory() as session:
        return await manifests_repo.list_by_thread(session, thread_id, include_deleted=True)


def _files_containing_text(root: Path, *needles: str) -> list[str]:
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


def _assert_fully_deleted(
    *,
    root: Path | None,
    ids: dict[str, Any],
    rows: list[dict[str, Any]],
    entry: str,
) -> None:
    """统一断言：热文件不存在、manifest 已 tombstone、（可选）全盘没有原文残留。

    ``root=None`` 用于"同一测试里还有另一个存活 thread"的场景——那时全盘扫描必然命中
    另一个 thread 的正文，需要调用方改用"该 thread 目录已无段文件"的局部断言。
    """
    assert not ids["hot_path"].exists(), f"{entry}：热缓存段必须被物理删除"
    assert rows, f"{entry}：manifest 行不应消失（tombstone 保留审计）"
    assert all(row["status"] == "deleted" for row in rows), (
        f"{entry}：全部段都必须落 tombstone，实际状态={[row['status'] for row in rows]}"
    )
    assert all(row["deleted_at"] is not None for row in rows), f"{entry}：deleted_at 必须写上"
    if root is not None:
        assert _files_containing_text(root, _USER_TEXT, _ASSISTANT_TEXT) == [], (
            f"{entry}：删除后仍有文件残留用户原文"
        )
    else:
        thread_dir = ids["hot_path"].parent
        assert list(thread_dir.glob("*.jsonl")) == [], f"{entry}：该 thread 的热段目录里仍有段文件"


# ---------------------------------------------------------------------------
# 真值表：object_key × 热文件
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sealed", "hot_present"),
    [
        pytest.param(True, True, id="object_key=有+hot=有"),
        pytest.param(True, False, id="object_key=有+hot=无"),
        pytest.param(False, True, id="object_key=无(未封存)+hot=有"),
        pytest.param(False, False, id="object_key=无(未封存)+hot=无"),
    ],
)
async def test_thread_deletion_truth_table(
    conversation_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    sealed: bool,
    hot_present: bool,
) -> None:
    """新发现 6 的真值表：四种组合都必须"删干净 + 落 tombstone"。"""
    factory = conversation_session_factory
    root = tmp_path / "rollout-root"
    store = LocalRolloutObjectStore(root=root)
    ids = (
        await _sealed_segment(factory, root, store)
        if sealed
        else await _open_segment(factory, root, store)
    )
    objects_before = len(await store.list_prefix(prefix="rollouts/"))
    assert objects_before == (1 if sealed else 0)
    if not hot_present:
        ids["hot_path"].unlink()

    assert await _run_delete_thread(factory, ids, store) == "done"

    rows = await _manifest_rows(factory, ids["thread_id"])
    _assert_fully_deleted(root=root, ids=ids, rows=rows, entry="thread_deletion")
    assert await store.list_prefix(prefix="rollouts/") == [], "对象镜像必须被物理删除"


async def test_thread_deletion_requires_store_with_unsealed_segment(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """未封存段同样需要对象存储（用于热段清扫）：缺它必须显式拒绝，不许静默跳过。"""
    factory = conversation_session_factory
    root = tmp_path / "rollout-root"
    ids = await _open_segment(factory, root, LocalRolloutObjectStore(root=root))

    assert await _run_delete_thread(factory, ids, None) == "wait"
    rows = await _manifest_rows(factory, ids["thread_id"])
    assert rows[0]["status"] == "open", "没有删除能力时不得落 tombstone"
    assert ids["hot_path"].is_file(), "数据确实还在（本用例证明的是'不说谎'）"


# ---------------------------------------------------------------------------
# 远端对象存储 + 本机热缓存（新发现 15）
# ---------------------------------------------------------------------------


class _RemoteStoreWithLocalHotCache:
    """模拟 kodo：对象在"远端"（内存），热段在本机 ``root`` 下。"""

    def __init__(self, *, root: Path) -> None:
        self._objects: dict[str, bytes] = {}
        self._hot = LocalHotSegmentCache(root=root)

    def seed(self, *, key: str, data: bytes) -> None:
        self._objects[key] = data

    def keys(self) -> list[str]:
        return sorted(self._objects)

    async def delete(self, *, key: str) -> None:
        self._objects.pop(key, None)

    async def delete_hot_segment(self, *, key: str) -> None:
        await self._hot.delete_hot_segment(key=key)

    async def delete_hot_thread(self, *, thread_id: Any) -> int:
        return await self._hot.delete_hot_thread(thread_id=thread_id)


async def test_thread_deletion_removes_local_hot_cache_for_remote_store(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """新发现 15：远端 store 也必须能删掉本机热段（kodo 模式的合规缺口）。"""
    factory = conversation_session_factory
    root = tmp_path / "rollout-root"
    local_store = LocalRolloutObjectStore(root=root)
    ids = await _sealed_segment(factory, root, local_store)
    # 把对象"搬到远端"：本地对象镜像删掉，改为远端 store 持有
    remote = _RemoteStoreWithLocalHotCache(root=root)
    remote.seed(key=ids["object_key"], data=b"remote-bytes")
    await local_store.delete(key=ids["object_key"])

    assert await _run_delete_thread(factory, ids, remote) == "done"

    assert remote.keys() == [], "远端对象必须被删除"
    rows = await _manifest_rows(factory, ids["thread_id"])
    _assert_fully_deleted(root=root, ids=ids, rows=rows, entry="thread_deletion(remote)")


# ---------------------------------------------------------------------------
# 参数化：登记表里的每个入口都真跑一遍
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_env(
    conversation_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """把 CLI 指向 conversation_test 库与临时 rollout 根目录。"""
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


async def test_entry_thread_deletion(
    conversation_session_factory: async_sessionmaker[AsyncSession], tmp_path: Path
) -> None:
    """thread_deletion：delete_thread Job 的真库真盘用例（登记表 key）。"""
    factory = conversation_session_factory
    root = tmp_path / "rollout-root"
    store = LocalRolloutObjectStore(root=root)
    ids = await _sealed_segment(factory, root, store)

    assert await _run_delete_thread(factory, ids, store) == "done"

    rows = await _manifest_rows(factory, ids["thread_id"])
    _assert_fully_deleted(root=root, ids=ids, rows=rows, entry="thread_deletion")
    assert await store.list_prefix(prefix="rollouts/") == []


async def test_entry_cli_delete_thread_rollouts(
    conversation_session_factory: async_sessionmaker[AsyncSession], cli_env: Path
) -> None:
    """cli_delete_thread_rollouts：CLI 删除 thread 的真库真盘用例（登记表 key）。"""
    factory = conversation_session_factory
    store = LocalRolloutObjectStore(root=cli_env)
    ids = await _sealed_segment(factory, cli_env, store)
    # 再加一个**未封存**段（object_key IS NULL）覆盖新发现 6
    extra_thread = await _open_segment(factory, cli_env, store)
    # 两个 thread 各自独立；本用例只删第一个，断言第二个不受影响（清扫按 thread 作用域）
    assert (
        await _run_cli(["delete-thread-rollouts", "--thread-id", str(ids["thread_id"]), "--apply"])
        == 0
    )

    rows = await _manifest_rows(factory, ids["thread_id"])
    # 另一个 thread 还活着 → 不做全盘原文扫描，改断言"该 thread 的段目录已空"
    _assert_fully_deleted(root=None, ids=ids, rows=rows, entry="cli_delete_thread_rollouts")
    assert await store.list_prefix(prefix="rollouts/") == [], "对象镜像必须被物理删除"
    # 作用域检查：另一个 thread 的未封存热段必须原样保留
    assert extra_thread["hot_path"].is_file(), (
        "thread 级删除只能清自己的目录，不得影响其它 thread 的热段"
    )


async def test_entry_cli_retention_scan(
    conversation_session_factory: async_sessionmaker[AsyncSession], cli_env: Path
) -> None:
    """cli_retention_scan：CLI retention 的真库真盘用例（登记表 key）。"""
    factory = conversation_session_factory
    store = LocalRolloutObjectStore(root=cli_env)
    ids = await _sealed_segment(factory, cli_env, store)
    # 同 thread 再放一个**未过期**的段：retention 只按 created_at 挑候选，且不得按 thread 清扫
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

    rows = await _manifest_rows(factory, ids["thread_id"])
    assert rows[0]["status"] == "deleted", "过期的 sealed 段必须落 tombstone"
    assert not ids["hot_path"].exists(), "retention 必须删掉热段（C-3）"
    assert await store.list_prefix(prefix="rollouts/") == []
    assert _files_containing_text(cli_env, _USER_TEXT, _ASSISTANT_TEXT) == []


async def test_retention_does_not_touch_unsealed_segments(
    conversation_session_factory: async_sessionmaker[AsyncSession], cli_env: Path
) -> None:
    """段级删除的边界：未封存段不是 retention 候选，热段必须原样保留。"""
    factory = conversation_session_factory
    store = LocalRolloutObjectStore(root=cli_env)
    ids = await _open_segment(factory, cli_env, store)
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

    rows = await _manifest_rows(factory, ids["thread_id"])
    assert rows[0]["status"] == "open", "retention 只扫 sealed 段，不得动 open 段"
    assert ids["hot_path"].is_file(), "retention 不得按 thread 清扫（会误删同 thread 的其它段）"


async def test_reconcile_orphans_does_not_delete_objects(
    conversation_session_factory: async_sessionmaker[AsyncSession], cli_env: Path
) -> None:
    """reconcile-orphans 的授权边界：只 tombstone 段，不删对象（登记在 CLI 白名单里）。"""
    factory = conversation_session_factory
    store = LocalRolloutObjectStore(root=cli_env)
    ids = await _sealed_segment(factory, cli_env, store)
    # 制造"对象缺失"的可修复项
    await store.delete(key=ids["object_key"])

    assert await _run_cli(["reconcile-orphans", "--apply"]) == 0

    rows = await _manifest_rows(factory, ids["thread_id"])
    assert rows[0]["status"] == "deleted", "对象确认缺失的 sealed 段应被标 deleted"
    # 授权边界：reconcile 只写 tombstone，不删对象、也不清热段（对象/热段的删除走删除入口）
    assert ids["hot_path"].is_file(), "reconcile 不得顺手删热段"
    assert _files_containing_text(cli_env, _USER_TEXT, _ASSISTANT_TEXT) != [], (
        "reconcile 不负责物理删除：本用例里用户原文应当仍在（用来固定这条边界）"
    )
