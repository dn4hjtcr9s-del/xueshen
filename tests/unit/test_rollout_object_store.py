"""Rollout 对象存储（Local / Fake）单元测试（memory-rebuild §5.5 / §5.11）。

重点验证"不可变 + 原子写"这两条让 Phase 3 切到 Kodo 后上层行为不变的语义：
同 key 同内容幂等、同 key 异内容拒绝、写入原子、range 与 key 校验。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from backend.conversation.contracts.object_store import (
    ObjectHashMismatchError,
    ObjectNotFoundError,
    ObjectStoreNonRetryableError,
    RolloutObjectStore,
)
from backend.conversation.rollout.object_store import (
    ROLLOUT_OBJECT_PREFIX,
    FakeRolloutObjectStore,
    LocalRolloutObjectStore,
    build_object_key,
    local_path_for_object_key,
)

_NOW = datetime(2026, 9, 10, 5, 0, 0, tzinfo=UTC)


def _key() -> str:
    return build_object_key(
        thread_created_at=_NOW,
        thread_id=uuid.UUID("11111111-1111-4111-8111-111111111111"),
        ordinal_start=3,
        segment_id=uuid.UUID("22222222-2222-4222-8222-222222222222"),
    )


# ---------------------------------------------------------------------------
# key 与本地路径
# ---------------------------------------------------------------------------


def test_object_key_shape_matches_spec() -> None:
    """§5.5：固定前缀 + 不可变 segment 标识，key 中不含用户可控原文。"""
    key = _key()
    assert key.startswith(f"{ROLLOUT_OBJECT_PREFIX}/threads/2026/09/10/")
    assert key.endswith("000000000003-22222222-2222-4222-8222-222222222222.jsonl")


def test_object_key_uses_utc_date() -> None:
    from datetime import timedelta, timezone

    tz = timezone(timedelta(hours=8))
    # 东八区 2026-09-10 01:00 == UTC 2026-09-09 17:00
    key = build_object_key(
        thread_created_at=datetime(2026, 9, 10, 1, 0, tzinfo=tz),
        thread_id=uuid.uuid4(),
        ordinal_start=0,
        segment_id=uuid.uuid4(),
    )
    assert "/2026/09/09/" in key


def test_object_key_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        build_object_key(
            thread_created_at=datetime(2026, 9, 10),
            thread_id=uuid.uuid4(),
            ordinal_start=0,
            segment_id=uuid.uuid4(),
        )


def test_local_path_for_object_key_mirrors_key(tmp_path: Path) -> None:
    """本地热缓存路径与对象 key 同构，封存后可由 key 精确还原位置。"""
    key = _key()
    local = local_path_for_object_key(root=tmp_path, object_key=key)
    assert str(local).startswith(str(tmp_path))
    assert local.parts[-1] == key.rsplit("/", 1)[1]
    assert "rollouts" not in local.parts


def test_local_path_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(ObjectStoreNonRetryableError):
        local_path_for_object_key(root=tmp_path, object_key="rollouts/../../etc/passwd")
    with pytest.raises(ObjectStoreNonRetryableError):
        local_path_for_object_key(root=tmp_path, object_key="/abs/path.jsonl")


# ---------------------------------------------------------------------------
# Local 实现
# ---------------------------------------------------------------------------


@pytest.fixture(params=["local", "fake"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    """同一套断言跑 Local 与 Fake，保证二者语义一致。"""
    if request.param == "local":
        return LocalRolloutObjectStore(root=tmp_path)
    return FakeRolloutObjectStore()


async def test_put_then_get_round_trip(store: Any) -> None:
    key = _key()
    ref = await store.put_immutable(key=key, data=b'{"ordinal": 0}\n')
    assert ref.size == len(b'{"ordinal": 0}\n')
    assert len(ref.sha256) == 64
    assert ref.sha256 == ref.sha256.lower()
    assert await store.get(key=key) == b'{"ordinal": 0}\n'


async def test_put_same_content_is_idempotent(store: Any) -> None:
    key = _key()
    first = await store.put_immutable(key=key, data=b"same")
    second = await store.put_immutable(key=key, data=b"same")
    assert first.sha256 == second.sha256
    assert first.etag == second.etag


async def test_put_different_content_same_key_is_rejected(store: Any) -> None:
    """段封存后不可变：同 key 异内容必须报错，不能静默覆盖。"""
    key = _key()
    await store.put_immutable(key=key, data=b"first")
    with pytest.raises(ObjectHashMismatchError):
        await store.put_immutable(key=key, data=b"second")


async def test_etag_differs_from_sha256(store: Any) -> None:
    """ETag 是服务端概念（Local 用内容 MD5 模拟），与内容 sha256 不同源。"""
    ref = await store.put_immutable(key=_key(), data=b"payload")
    assert ref.etag != ref.sha256


async def test_get_missing_object_raises_not_found(store: Any) -> None:
    with pytest.raises(ObjectNotFoundError):
        await store.get(key="rollouts/threads/2026/09/10/x/missing.jsonl")


async def test_head_returns_metadata_without_sha256(store: Any) -> None:
    key = _key()
    await store.put_immutable(key=key, data=b"abcdef")
    stat = await store.head(key=key)
    assert stat.size == 6
    assert "sha256" not in stat.model_dump()


async def test_get_range_returns_exact_slice(store: Any) -> None:
    key = _key()
    await store.put_immutable(key=key, data=b"0123456789")
    assert await store.get_range(key=key, offset=2, length=3) == b"234"


async def test_get_range_beyond_end_is_rejected(store: Any) -> None:
    key = _key()
    await store.put_immutable(key=key, data=b"0123")
    with pytest.raises(ObjectStoreNonRetryableError):
        await store.get_range(key=key, offset=2, length=99)


async def test_list_prefix_filters_by_prefix(store: Any) -> None:
    await store.put_immutable(key=_key(), data=b"a")
    await store.put_immutable(
        key=build_object_key(
            thread_created_at=_NOW,
            thread_id=uuid.uuid4(),
            ordinal_start=0,
            segment_id=uuid.uuid4(),
        ),
        data=b"b",
    )
    all_stats = await store.list_prefix(prefix=f"{ROLLOUT_OBJECT_PREFIX}/")
    assert len(all_stats) == 2
    assert await store.list_prefix(prefix="nowhere/") == []


async def test_delete_is_idempotent(store: Any) -> None:
    key = _key()
    await store.put_immutable(key=key, data=b"x")
    await store.delete(key=key)
    await store.delete(key=key)  # 重复删除不报错
    with pytest.raises(ObjectNotFoundError):
        await store.get(key=key)


async def test_health_check(store: Any) -> None:
    assert await store.health_check() is True


async def test_both_implementations_satisfy_protocol(tmp_path: Path) -> None:
    assert isinstance(LocalRolloutObjectStore(root=tmp_path), RolloutObjectStore)
    assert isinstance(FakeRolloutObjectStore(), RolloutObjectStore)


# ---------------------------------------------------------------------------
# Local 特有的原子写与 Fake 的故障注入
# ---------------------------------------------------------------------------


async def test_local_write_is_atomic_no_temp_left_behind(tmp_path: Path) -> None:
    store = LocalRolloutObjectStore(root=tmp_path)
    await store.put_immutable(key=_key(), data=b"data")
    leftovers = [p for p in (tmp_path / "objects").rglob("*.tmp-*")]
    assert leftovers == []


def test_fake_can_simulate_missing_and_tampered_objects() -> None:
    """reconcile 的两类关键故障必须能在测试里稳定复现。"""
    import asyncio

    store = FakeRolloutObjectStore()
    key = _key()
    asyncio.run(store.put_immutable(key=key, data=b"original"))
    before = asyncio.run(store.head(key=key))
    store.corrupt(key)
    after = asyncio.run(store.head(key=key))
    assert after.size != before.size, "篡改后大小应变化，供 checksum 不一致用例断言"

    store.drop(key)
    with pytest.raises(ObjectNotFoundError):
        asyncio.run(store.get(key=key))


def test_fake_records_put_calls() -> None:
    import asyncio

    store = FakeRolloutObjectStore()
    asyncio.run(store.put_immutable(key=_key(), data=b"x"))
    assert store.put_calls == 1
