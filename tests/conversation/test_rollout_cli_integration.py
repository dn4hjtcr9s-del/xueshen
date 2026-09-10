"""Rollout 运维 CLI 集成测试（memory-rebuild §5.4 / §5.5 / OPEN-005）。

补上 `cli/rollout.py` 五个子命令的仓库内集成测试。此前只做过手工冒烟，
没有回归保护；这里让 CLI 真正连 conversation_test 库并操作真实对象存储。

覆盖：verify-manifest（干净/脏两态退出码）、reconcile-orphans（dry-run 无副作用）、
export-thread（按 ordinal 拼接、跳过缺失段）、delete-thread-rollouts（dry-run 与
--apply）、retention-scan（阈值筛选与 --apply）、非法参数退出码。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.cli import rollout as rollout_cli
from backend.conversation.persistence import rollout_manifests as manifests_repo
from backend.conversation.rollout.object_store import (
    LocalRolloutObjectStore,
    build_object_key,
)

pytest_plugins = ("tests.conversation.conftest",)

_NOW = datetime(2026, 9, 10, 5, 0, 0, tzinfo=UTC)


@pytest.fixture()
def cli_env(
    conversation_settings: Any,
    conversation_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """把 CLI 指向 conversation_test 库与临时 rollout 根目录。

    CLI 通过 ``get_settings()`` 自装配依赖，因此这里改环境变量并清缓存。
    """
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


async def _run(argv: list[str]) -> int:
    """按 CLI 的真实入口执行一条子命令，返回退出码。

    测试本身在事件循环里跑，因此这里直接 await，不能再套 asyncio.run。
    """
    args = rollout_cli.build_parser().parse_args(argv)
    return await rollout_cli._invoke_handler(args.func, args)


def _seed_records(count: int, *, start_ordinal: int = 0) -> bytes:
    """造 count 条记录。

    ``ordinal`` 是 **thread 级连续**的（Phase 1 决策），因此同 thread 的第二个段
    必须接着上一段的序号，不能用 0 重新开始——否则导出顺序断言会失真。
    """
    lines = []
    for ordinal in range(start_ordinal, start_ordinal + count):
        lines.append(
            json.dumps(
                {
                    "recorded_at": "2026-09-10T05:00:00.000Z",
                    "ordinal": ordinal,
                    "type": "thread_meta" if ordinal == 0 else "turn_started",
                    "turn_id": None if ordinal == 0 else str(uuid.uuid4()),
                    "payload": {}
                    if ordinal == 0
                    else {
                        "turn_id": str(uuid.uuid4()),
                        "request_id": "r",
                        "started_at": "2026-09-10T05:00:00+00:00",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return "".join(lines).encode()


async def _seed_thread_with_segments(
    factory: async_sessionmaker[AsyncSession],
    store: LocalRolloutObjectStore,
    *,
    segment_count: int = 2,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """造一个 thread，含 N 个已封存段（各自独立 turn）与对应对象。"""
    thread_id, user_id = uuid.uuid4(), uuid.uuid4()
    now = created_at or _NOW
    segment_ids: list[uuid.UUID] = []
    object_keys: list[str] = []
    async with factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO conversation.conversation_threads (thread_id, user_id) "
                    "VALUES (:thread_id, :user_id)"
                ),
                {"thread_id": thread_id, "user_id": user_id},
            )
    for index in range(segment_count):
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
                        "thread_id": thread_id,
                        "user_id": user_id,
                        "client_request_id": str(turn_id),
                        "now": now,
                    },
                )
        data = _seed_records(2, start_ordinal=index * 2)
        object_key = build_object_key(
            thread_created_at=now,
            thread_id=thread_id,
            ordinal_start=index * 2,
            segment_id=uuid.uuid4(),
        )
        await store.put_immutable(key=object_key, data=data)
        segment_id = uuid.uuid4()
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO conversation.conversation_rollout_segments ("
                        "segment_id, thread_id, turn_id, ordinal_start, ordinal_end, "
                        "object_key, object_etag, sha256, byte_size, status, created_at, sealed_at"
                        ") VALUES ("
                        ":segment_id, :thread_id, :turn_id, :ordinal_start, :ordinal_end, "
                        ":object_key, 'etag', :sha256, :byte_size, 'sealed', :now, :now)"
                    ),
                    {
                        "segment_id": segment_id,
                        "thread_id": thread_id,
                        "turn_id": turn_id,
                        "ordinal_start": index * 2,
                        "ordinal_end": index * 2 + 1,
                        "object_key": object_key,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "byte_size": len(data),
                        "now": now,
                    },
                )
        segment_ids.append(segment_id)
        object_keys.append(object_key)
    return {
        "thread_id": thread_id,
        "user_id": user_id,
        "segment_ids": segment_ids,
        "object_keys": object_keys,
    }


# ---------------------------------------------------------------------------
# verify-manifest
# ---------------------------------------------------------------------------


async def test_verify_manifest_clean_returns_zero(
    conversation_session_factory: async_sessionmaker[AsyncSession],
    cli_env: Path,
) -> None:
    store = LocalRolloutObjectStore(root=cli_env)
    ids = await _seed_thread_with_segments(conversation_session_factory, store)
    assert await _run(["verify-manifest"]) == 0, "一致状态必须退出 0，便于 CI 断言"
    assert ids["object_keys"]


async def test_verify_manifest_reports_inconsistency_with_exit_one(
    conversation_session_factory: async_sessionmaker[AsyncSession],
    cli_env: Path,
) -> None:
    store = LocalRolloutObjectStore(root=cli_env)
    ids = await _seed_thread_with_segments(conversation_session_factory, store)
    # 制造孤儿对象（无 manifest 引用）
    await store.put_immutable(
        key=build_object_key(
            thread_created_at=_NOW,
            thread_id=uuid.uuid4(),
            ordinal_start=0,
            segment_id=uuid.uuid4(),
        ),
        data=b'{"ordinal": 0}\n',
    )
    assert await _run(["verify-manifest"]) == 1, "发现不一致必须退出 1"
    assert ids["object_keys"]


# ---------------------------------------------------------------------------
# reconcile-orphans
# ---------------------------------------------------------------------------


async def test_reconcile_orphans_defaults_to_dry_run(
    conversation_session_factory: async_sessionmaker[AsyncSession],
    cli_env: Path,
) -> None:
    factory = conversation_session_factory
    store = LocalRolloutObjectStore(root=cli_env)
    ids = await _seed_thread_with_segments(factory, store)
    # 删掉一个对象 → 该 sealed 段成为"对象缺失"，属可自动修复类
    await store.delete(key=ids["object_keys"][0])

    assert await _run(["reconcile-orphans"]) == 0
    async with factory() as session:
        row = await manifests_repo.get_by_id(session, ids["segment_ids"][0])
    assert row is not None and row["status"] == "sealed", "默认 dry-run 不得改状态"

    assert await _run(["reconcile-orphans", "--apply"]) == 0
    async with factory() as session:
        row = await manifests_repo.get_by_id(session, ids["segment_ids"][0])
    assert row is not None and row["status"] == "deleted", "--apply 才真正标记"


# ---------------------------------------------------------------------------
# export-thread
# ---------------------------------------------------------------------------


async def test_export_thread_concatenates_segments_in_order(
    conversation_session_factory: async_sessionmaker[AsyncSession],
    cli_env: Path,
) -> None:
    store = LocalRolloutObjectStore(root=cli_env)
    ids = await _seed_thread_with_segments(conversation_session_factory, store, segment_count=2)
    out = cli_env / "export.jsonl"

    assert (
        await _run(["export-thread", "--thread-id", str(ids["thread_id"]), "--output", str(out)])
        == 0
    )
    lines = [line for line in out.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 4, "两个段各 2 行"
    ordinals = [json.loads(line)["ordinal"] for line in lines]
    assert ordinals == sorted(ordinals), "必须按 ordinal 有序拼接"


async def test_export_thread_skips_missing_segment_without_failing(
    conversation_session_factory: async_sessionmaker[AsyncSession],
    cli_env: Path,
) -> None:
    store = LocalRolloutObjectStore(root=cli_env)
    ids = await _seed_thread_with_segments(conversation_session_factory, store, segment_count=2)
    await store.delete(key=ids["object_keys"][1])
    out = cli_env / "export-partial.jsonl"

    assert (
        await _run(["export-thread", "--thread-id", str(ids["thread_id"]), "--output", str(out)])
        == 0
    )
    lines = [line for line in out.read_text(encoding="utf-8").splitlines() if line]
    assert len(lines) == 2, "缺失段跳过，其余照常导出"


# ---------------------------------------------------------------------------
# delete-thread-rollouts
# ---------------------------------------------------------------------------


async def test_delete_thread_rollouts_dry_run_then_apply(
    conversation_session_factory: async_sessionmaker[AsyncSession],
    cli_env: Path,
) -> None:
    factory = conversation_session_factory
    store = LocalRolloutObjectStore(root=cli_env)
    ids = await _seed_thread_with_segments(factory, store)

    assert await _run(["delete-thread-rollouts", "--thread-id", str(ids["thread_id"])]) == 0
    assert len(await store.list_prefix(prefix="rollouts/")) == 2, "dry-run 不得删对象"

    assert (
        await _run(["delete-thread-rollouts", "--thread-id", str(ids["thread_id"]), "--apply"]) == 0
    )
    assert await store.list_prefix(prefix="rollouts/") == [], "--apply 必须物理删除对象"
    async with factory() as session:
        rows = await manifests_repo.list_by_thread(session, ids["thread_id"], include_deleted=True)
    assert all(row["status"] == "deleted" for row in rows)
    assert all(row["deleted_at"] is not None for row in rows)


# ---------------------------------------------------------------------------
# retention-scan
# ---------------------------------------------------------------------------


async def test_retention_scan_only_touches_segments_older_than_threshold(
    conversation_session_factory: async_sessionmaker[AsyncSession],
    cli_env: Path,
) -> None:
    factory = conversation_session_factory
    store = LocalRolloutObjectStore(root=cli_env)
    old = await _seed_thread_with_segments(
        factory, store, segment_count=1, created_at=datetime.now(UTC) - timedelta(days=90)
    )
    fresh = await _seed_thread_with_segments(factory, store, segment_count=1)

    assert await _run(["retention-scan", "--older-than-days", "30"]) == 0
    assert len(await store.list_prefix(prefix="rollouts/")) == 2, "dry-run 不删对象"

    assert await _run(["retention-scan", "--older-than-days", "30", "--apply"]) == 0
    remaining = [stat.key for stat in await store.list_prefix(prefix="rollouts/")]
    assert old["object_keys"][0] not in remaining, "超期段的对象应被删除"
    assert fresh["object_keys"][0] in remaining, "未超期段必须保留"

    async with factory() as session:
        old_row = await manifests_repo.get_by_id(session, old["segment_ids"][0])
        fresh_row = await manifests_repo.get_by_id(session, fresh["segment_ids"][0])
    assert old_row is not None and old_row["status"] == "deleted"
    assert fresh_row is not None and fresh_row["status"] == "sealed"


# ---------------------------------------------------------------------------
# 参数校验与配置门禁
# ---------------------------------------------------------------------------


def test_invalid_older_than_days_exits_two() -> None:
    """非法阈值由 argparse 以退出码 2 拒绝（不是静默按 0 处理）。"""
    with pytest.raises(SystemExit) as excinfo:
        rollout_cli.build_parser().parse_args(["retention-scan", "--older-than-days", "-1"])
    assert excinfo.value.code == 2


def test_missing_thread_id_exits_two() -> None:
    with pytest.raises(SystemExit) as excinfo:
        rollout_cli.build_parser().parse_args(["export-thread"])
    assert excinfo.value.code == 2


async def test_cli_refuses_kodo_when_config_incomplete(
    conversation_settings: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kodo 模式配置不全时必须显式失败，**不静默降级为 local**（§5.12）。

    Kodo 适配器已在 Phase 3 落地，因此"模式本身"不再被拒；被拒的是**缺配置**。
    完整配置下的真实连通性由 smoke 测试负责（无凭据时 skip）。
    """
    monkeypatch.setenv(
        "CONVERSATION_DATABASE_URL", str(conversation_settings.conversation_database_url)
    )
    monkeypatch.setenv("CONVERSATION_ROLLOUT_ROOT", str(tmp_path))
    monkeypatch.setenv("CONVERSATION_ROLLOUT_OBJECT_STORE", "kodo")
    # 刻意缺 KODO_BUCKET
    monkeypatch.setenv("KODO_ACCESS_KEY", "ak")
    monkeypatch.setenv("KODO_SECRET_KEY", "sk")
    monkeypatch.delenv("KODO_BUCKET", raising=False)
    from backend.settings import get_settings

    get_settings.cache_clear()
    try:
        # Settings 层（Phase 0 加的校验）在**构造时**就拦住，比 CLI 更早失败——
        # 这是更强的保证：配置不全连进程都起不来，不可能带着坏配置跑起来。
        with pytest.raises(ValidationError, match="KODO_BUCKET"):
            await _run(["verify-manifest"])
    finally:
        get_settings.cache_clear()
