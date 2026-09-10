"""Rollout 对象存储实现：Local（目录模拟 bucket）与 Fake（内存，测试用）。

memory-rebuild §5.4 Phase 2 / §5.5。业务层只依赖
``contracts/object_store.py`` 的 :class:`RolloutObjectStore` 协议与 ``ObjectRef``；
七牛 Kodo 适配器属 Phase 3，届时只需新增一个实现，不改 reader / manifest / 封存逻辑。

**Local 不是"把本地文件名当 object key"**：它按 bucket 语义把 object key 映射到
``{root}/objects/<key>``，写入走"临时文件 → fsync → 原子 rename"，并对同 key 异内容
报 :class:`ObjectHashMismatchError`——与真实对象存储的不可变语义一致，这样 Phase 3
切到 Kodo 时上层行为不变。

**etag 与 sha256 刻意不同源**：Local 用内容 MD5 模拟 S3/Kodo 单段对象的 ETag，sha256
由调用方本地计算。二者语义不同（ETag 是服务端概念），因此不能互相冒充——这正是
``ObjectRef`` 把它们分成两个字段的原因。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from backend.conversation.contracts.object_store import (
    ObjectHashMismatchError,
    ObjectNotFoundError,
    ObjectRef,
    ObjectStat,
    ObjectStoreNonRetryableError,
)

#: 对象 key 的固定前缀（§5.5）。
ROLLOUT_OBJECT_PREFIX = "rollouts"

#: 对象内容类型（JSONL 段）。
ROLLOUT_CONTENT_TYPE = "application/x-ndjson"

#: Local 实现中 bucket 语义根目录名（与本地热缓存目录区分开）。
_OBJECTS_DIRNAME = "objects"


def build_object_key(
    *,
    thread_created_at: datetime,
    thread_id: UUID | str,
    ordinal_start: int,
    segment_id: UUID | str,
) -> str:
    """段的对象 key。

    形如 ``rollouts/threads/YYYY/MM/DD/<thread_id>/<ordinal_start>-<segment_id>.jsonl``。

    key 与本地热缓存路径同构（只差前缀与根目录），因此封存时不需要再做一次映射。
    key 中不放任何用户可控原文（thread_id/segment_id 都是 UUID）。
    """
    if thread_created_at.tzinfo is None:
        raise ValueError("thread_created_at 必须带时区")
    created = thread_created_at.astimezone(UTC)
    return (
        f"{ROLLOUT_OBJECT_PREFIX}/threads/{created:%Y}/{created:%m}/{created:%d}"
        f"/{thread_id}/{ordinal_start:012d}-{segment_id}.jsonl"
    )


def _validate_prefix(prefix: str) -> str:
    """校验列表前缀：允许结尾斜杠（``rollouts/`` 是合法的目录前缀），其余同 key。"""
    if not prefix:
        return prefix
    return _validate_key(prefix.rstrip("/")) + "/"


def local_path_for_object_key(*, root: str | Path, object_key: str) -> Path:
    """对象 key → 本地热缓存路径。

    本地段路径与对象 key 同构（只差 ``rollouts/`` 前缀与根目录），因此封存后仍能
    由 manifest 的 ``object_key`` 精确还原本地文件位置——**不需要**依赖 thread 创建
    时间反推日期目录（manifest 只记段创建时间，跨零点时两者可能不同日）。
    """
    _validate_key(object_key)
    prefix = f"{ROLLOUT_OBJECT_PREFIX}/"
    relative = object_key[len(prefix) :] if object_key.startswith(prefix) else object_key
    return Path(root) / relative


def _validate_key(key: str) -> str:
    """拒绝绝对路径与路径穿越：object key 必须始终落在 bucket 内。"""
    if not key or key.startswith("/") or "\\" in key:
        raise ObjectStoreNonRetryableError(f"非法 object key: {key!r}")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ObjectStoreNonRetryableError(f"object key 含非法路径段: {key!r}")
    return key


class LocalRolloutObjectStore:
    """本地目录模拟对象存储（开发与 Phase 2/3 的默认实现）。"""

    def __init__(self, *, root: str | Path) -> None:
        self._root = Path(root) / _OBJECTS_DIRNAME

    # -- 内部 --------------------------------------------------------------

    def _path_for(self, key: str) -> Path:
        return self._root / _validate_key(key)

    def _etag_for(self, data: bytes) -> str:
        """以内容 MD5 模拟服务端 ETag（区别于内容 sha256）。"""
        return hashlib.md5(data).hexdigest()

    def _read(self, key: str) -> bytes:
        path = self._path_for(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectNotFoundError(f"对象不存在: {key}") from exc

    # -- 协议 --------------------------------------------------------------

    async def put_immutable(self, *, key: str, data: bytes) -> ObjectRef:
        """不可变写入：同 key 同内容幂等返回；同 key 异内容拒绝覆盖。"""
        path = self._path_for(key)
        sha256 = hashlib.sha256(data).hexdigest()
        if path.exists():
            existing = path.read_bytes()
            if hashlib.sha256(existing).hexdigest() != sha256:
                raise ObjectHashMismatchError(f"对象已存在且内容不同，拒绝覆盖: {key}")
            return ObjectRef(
                key=key,
                etag=self._etag_for(existing),
                sha256=sha256,
                size=len(existing),
                content_type=ROLLOUT_CONTENT_TYPE,
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        # 临时文件 + fsync + 原子 rename：避免崩溃留下半截对象
        tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        with tmp_path.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.replace(path)
        return ObjectRef(
            key=key,
            etag=self._etag_for(data),
            sha256=sha256,
            size=len(data),
            content_type=ROLLOUT_CONTENT_TYPE,
        )

    async def get(self, *, key: str) -> bytes:
        return self._read(key)

    async def get_range(self, *, key: str, offset: int, length: int) -> bytes:
        if offset < 0 or length < 0:
            raise ObjectStoreNonRetryableError("offset/length 不得为负")
        data = self._read(key)
        if offset + length > len(data):
            raise ObjectStoreNonRetryableError(
                f"range 越界: key={key} offset={offset} length={length} size={len(data)}"
            )
        return data[offset : offset + length]

    async def head(self, *, key: str) -> ObjectStat:
        path = self._path_for(key)
        if not path.is_file():
            raise ObjectNotFoundError(f"对象不存在: {key}")
        data = path.read_bytes()
        return ObjectStat(
            key=key,
            etag=self._etag_for(data),
            size=len(data),
            content_type=ROLLOUT_CONTENT_TYPE,
        )

    async def list_prefix(self, *, prefix: str, limit: int = 1000) -> list[ObjectStat]:
        # 直接拼路径：_path_for 会按 object key 再校验一次，而前缀允许结尾斜杠
        base = (self._root / _validate_prefix(prefix)) if prefix else self._root
        if not base.exists():
            return []
        stats: list[ObjectStat] = []
        for path in sorted(base.rglob("*.jsonl")):
            if len(stats) >= limit:
                break
            key = str(path.relative_to(self._root))
            stats.append(await self.head(key=key))
        return stats

    async def delete(self, *, key: str) -> None:
        """幂等删除；对象不存在视为成功（retention 重跑不应报错）。"""
        path = self._path_for(key)
        try:
            path.unlink()
        except FileNotFoundError:
            return

    async def presign_read(self, *, key: str, expires_seconds: int) -> str:
        """Local 无签名概念：返回 file URL，并显式说明不提供真实签名。"""
        _validate_key(key)
        return f"file://{self._path_for(key)}"

    async def health_check(self) -> bool:
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            probe = self._root / ".health"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            return False


class FakeRolloutObjectStore:
    """内存对象存储，供单元测试做故障注入（缺失/篡改/上传失败）。"""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        #: 置非 None 时 put_immutable 抛该异常，用于测试"上传成功但 PG 失败"的相邻分支
        self.put_error: Exception | None = None
        self.put_calls: int = 0

    def corrupt(self, key: str) -> None:
        """篡改对象内容，用于 reconcile 的 checksum 不一致用例。"""
        if key in self._objects:
            self._objects[key] = self._objects[key] + b"\n"
        else:
            raise ObjectNotFoundError(f"对象不存在: {key}")

    def drop(self, key: str) -> None:
        """删除对象但保留 manifest，制造"已登记但对象缺失"。"""
        self._objects.pop(key, None)

    def keys(self) -> list[str]:
        return sorted(self._objects)

    async def put_immutable(self, *, key: str, data: bytes) -> ObjectRef:
        self.put_calls += 1
        if self.put_error is not None:
            raise self.put_error
        sha256 = hashlib.sha256(data).hexdigest()
        existing = self._objects.get(key)
        if existing is not None and hashlib.sha256(existing).hexdigest() != sha256:
            raise ObjectHashMismatchError(f"对象已存在且内容不同，拒绝覆盖: {key}")
        self._objects[key] = data
        return ObjectRef(
            key=key,
            etag=hashlib.md5(data).hexdigest(),
            sha256=sha256,
            size=len(data),
            content_type=ROLLOUT_CONTENT_TYPE,
        )

    async def get(self, *, key: str) -> bytes:
        try:
            return self._objects[key]
        except KeyError as exc:
            raise ObjectNotFoundError(f"对象不存在: {key}") from exc

    async def get_range(self, *, key: str, offset: int, length: int) -> bytes:
        data = await self.get(key=key)
        if offset < 0 or length < 0 or offset + length > len(data):
            raise ObjectStoreNonRetryableError(f"range 越界: key={key}")
        return data[offset : offset + length]

    async def head(self, *, key: str) -> ObjectStat:
        data = await self.get(key=key)
        return ObjectStat(
            key=key,
            etag=hashlib.md5(data).hexdigest(),
            size=len(data),
            content_type=ROLLOUT_CONTENT_TYPE,
        )

    async def list_prefix(self, *, prefix: str, limit: int = 1000) -> list[ObjectStat]:
        stats = [
            ObjectStat(
                key=key,
                etag=hashlib.md5(data).hexdigest(),
                size=len(data),
                content_type=ROLLOUT_CONTENT_TYPE,
            )
            for key, data in sorted(self._objects.items())
            if key.startswith(prefix)
        ]
        return stats[:limit]

    async def delete(self, *, key: str) -> None:
        self._objects.pop(key, None)

    async def presign_read(self, *, key: str, expires_seconds: int) -> str:
        return f"fake://{key}?expires={expires_seconds}"

    async def health_check(self) -> bool:
        return True


def remove_local_object_root(*, root: str | Path) -> None:
    """测试辅助：整体清理 Local 对象目录。"""
    shutil.rmtree(Path(root) / _OBJECTS_DIRNAME, ignore_errors=True)
