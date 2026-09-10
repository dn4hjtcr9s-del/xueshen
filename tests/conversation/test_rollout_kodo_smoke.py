"""七牛 Kodo 真实网络 smoke（memory-rebuild §5.5 Phase 3 验收）。

**默认 skip**：只有 KODO_ACCESS_KEY / KODO_SECRET_KEY / KODO_BUCKET / KODO_REGION
四项齐备时才运行，因此 CI 在无账号时完全走 Local/Fake（§5.5：「七牛没有账号时，
CI 必须完全通过本地/Fake adapter；真正 Kodo smoke 只在凭据注入的受控环境运行」）。

**副作用边界**（用户 2026-09-13 确认：允许受控读写、测完自删）：
- 只在 ``rollouts/_smoke/<随机 uuid>/`` 前缀下写对象；
- 不读、不改、不删该前缀以外的任何对象；
- 无论成功失败都在 finally 里清理自己写下的 key。

本文件刻意**不请求任何数据库 fixture**，因此可以脱离 PostgreSQL 单独运行：

    KODO_ACCESS_KEY=... KODO_SECRET_KEY=... KODO_BUCKET=... KODO_REGION=z0 \\
      uv run pytest tests/conversation/test_rollout_kodo_smoke.py -q
"""

from __future__ import annotations

import hashlib
import os
import uuid

import pytest

from backend.conversation.contracts.object_store import (
    ObjectHashMismatchError,
    ObjectNotFoundError,
)
from backend.conversation.rollout.qiniu_kodo import QiniuKodoRolloutObjectStore

pytestmark = pytest.mark.skipif(
    not all(
        os.environ.get(name)
        for name in ("KODO_ACCESS_KEY", "KODO_SECRET_KEY", "KODO_BUCKET", "KODO_REGION")
    ),
    reason="未注入七牛凭据（KODO_ACCESS_KEY/SECRET_KEY/BUCKET/REGION），跳过真实网络 smoke",
)

_SMOKE_PREFIX = f"rollouts/_smoke/{uuid.uuid4()}/"


def _store() -> QiniuKodoRolloutObjectStore:
    return QiniuKodoRolloutObjectStore(
        bucket=os.environ["KODO_BUCKET"],
        access_key=os.environ["KODO_ACCESS_KEY"],
        secret_key=os.environ["KODO_SECRET_KEY"],
        region=os.environ.get("KODO_REGION", ""),
        domain=os.environ.get("KODO_CDN_DOMAIN", ""),
    )


async def test_kodo_round_trip_and_self_cleanup() -> None:
    """put → head → get → get_range → list → presign → delete 全链路，并自清理。"""
    store = _store()
    key = f"{_SMOKE_PREFIX}segment-0.jsonl"
    data = b'{"ordinal": 0, "type": "thread_meta", "turn_id": null, "payload": {}}\n'
    written: list[str] = []
    try:
        assert await store.health_check() is True

        ref = await store.put_immutable(key=key, data=data)
        written.append(key)
        assert ref.size == len(data)
        assert ref.sha256 == hashlib.sha256(data).hexdigest()
        assert ref.etag, "Kodo 应返回服务端 ETag"

        stat = await store.head(key=key)
        assert stat.size == len(data)
        assert stat.etag == ref.etag

        assert await store.get(key=key) == data

        chunk = await store.get_range(key=key, offset=0, length=10)
        assert chunk == data[:10]

        keys = [item.key for item in await store.list_prefix(prefix=_SMOKE_PREFIX)]
        assert key in keys

        url = await store.presign_read(key=key, expires_seconds=60)
        assert url.startswith("https://")

        await store.delete(key=key)
        written.remove(key)
        with pytest.raises(ObjectNotFoundError):
            await store.get(key=key)
    finally:
        for leftover in written:
            try:
                await store.delete(key=leftover)
            except Exception:  # pragma: no cover - 清理尽力而为
                pass


async def test_kodo_put_is_immutable() -> None:
    """同 key 二次上传必须被服务端 insertOnly 拒绝（段不可变）。"""
    store = _store()
    key = f"{_SMOKE_PREFIX}segment-immutable.jsonl"
    written: list[str] = []
    try:
        await store.put_immutable(key=key, data=b"first\n")
        written.append(key)
        with pytest.raises(ObjectHashMismatchError):
            await store.put_immutable(key=key, data=b"second\n")
    finally:
        for leftover in written:
            try:
                await store.delete(key=leftover)
            except Exception:  # pragma: no cover
                pass


async def test_kodo_delete_missing_object_is_idempotent() -> None:
    store = _store()
    await store.delete(key=f"{_SMOKE_PREFIX}never-existed.jsonl")
