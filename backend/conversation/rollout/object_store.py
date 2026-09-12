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
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from backend.conversation.contracts.object_store import (
    ObjectHashMismatchError,
    ObjectNotFoundError,
    ObjectRef,
    ObjectStat,
    ObjectStoreNonRetryableError,
)
from backend.conversation.rollout.file_naming import (
    iter_thread_dirs,
    normalize_thread_id,
    parse_segment_filename,
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


def delete_hot_segment_file(*, root: str | Path, object_key: str) -> bool:
    """删除与 ``object_key`` 同构的本地热缓存段文件（幂等）。

    返回是否真的删掉了文件（文件本就不存在时返回 ``False``）。文件系统错误（权限、
    IO）照常抛出：删除是合规动作，**不允许**退化成"只 warning 然后照样宣称删干净"。

    只删段文件本身，不清理空的日期目录——目录名不含用户数据，且清理目录会引入
    "删掉别的写者正在使用的目录"这种竞态。
    """
    path = local_path_for_object_key(root=root, object_key=object_key)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def delete_hot_thread_files(*, root: str | Path, thread_id: UUID | str) -> int:
    """删除该 thread 在**所有日期分片**下的热段文件，返回删除个数（review-2 新发现 6/15）。

    为什么需要 **root 级**（而不是继续按 object key 反查路径）：

    - **未封存段没有 object key**（``status='open'`` 行的 ``object_key`` 为 NULL）。
      按 key 推导的路子对这类行完全失效，删除路径会整段跳过它们，把用户原文留在磁盘上；
    - 记录器写热段用的日期目录来自**调用方传入的 thread 创建时间**（
      ``runner`` 在缺 thread 行时取 ``clock.now()``），与 ``conversation_threads.created_at``
      **不保证同日**；按"thread 行时间 + ordinal + segment_id"现算路径会算到别的日期目录。
      按 ``<thread_id>`` 目录反查与日期无关，是唯一稳健的定位方式；
    - kodo 模式下记录器**仍然**把热段写在本地（远端存储只能删对象镜像），
      这里同时是那条本地缓存的清理入口（新发现 15）。

    目录不存在、thread 目录不存在都返回 0（幂等：重跑删除命令不应报错）。非段文件
    （``.tmp``、回滚预留命名等，见 :func:`parse_segment_filename`）不动；空的日期/thread
    目录也不清理（与 :func:`delete_hot_segment_file` 同一理由：避免与其它写者竞态）。
    """
    removed = 0
    for thread_dir in iter_thread_dirs(root=root, thread_id=thread_id):
        for entry in thread_dir.iterdir():
            if not entry.is_file():
                continue
            if parse_segment_filename(entry.name) is None:
                continue
            entry.unlink()
            removed += 1
    return removed


@runtime_checkable
class HotSegmentDeleter(Protocol):
    """可选能力：删除 ``object_key`` 对应的**本地热缓存段文件**（§1.5 / §1.8）。

    recorder 把热段写在 ``conversation_rollout_root`` 下，与对象存储后端解耦；
    删除 thread / retention 必须让"对象镜像"与"热缓存"同时消失，否则 tombstone
    之后（``list_by_thread`` 过滤 deleted、``retention-scan`` 只扫 sealed、
    ``reconcile`` 只列 ``rollouts/`` 前缀）残留正文将**永久不可发现、不可清理**。

    factory 产出的 store **都**实现本协议（kodo 也会附带本地热缓存清理能力，
    见 ``LocalHotSegmentCache``），因此"没有该能力"不是合法状态，只出现在测试替身上。
    """

    async def delete_hot_segment(self, *, key: str) -> None: ...


@runtime_checkable
class HotSegmentRootDeleter(Protocol):
    """可选能力：按 **thread** 清理本地热缓存段目录（review-2 新发现 6/15）。

    与 :class:`HotSegmentDeleter` 的分工：

    - 按 key 删：已封存段的精确清理（retention、逐段删除）；
    - 按 thread 删：**未封存段**（没有 object key 可推导）与 kodo 模式的本地残留，
      只能在 thread 级别按目录清扫。调用方必须已经确认该 thread 没有活动 Turn
      （删除流程的 R4 前置条件），否则会误删正在写入的热段。
    """

    async def delete_hot_thread(self, *, thread_id: UUID | str) -> int: ...


async def delete_hot_segment(*, object_store: Any, key: str) -> bool:
    """调用对象存储的本地热缓存删除能力；实现不具备该能力时返回 ``False``。

    返回 ``False`` 只表示"该实现没有热缓存能力"，**不等于**"本机磁盘上没有热文件"
    （Kodo 模式下 recorder 仍会在 ``conversation_rollout_root`` 写本地热段），
    因此调用方不要把它当成"磁盘已无残留"的证明。
    """
    if not isinstance(object_store, HotSegmentDeleter):
        return False
    await object_store.delete_hot_segment(key=key)
    return True


async def delete_hot_thread_segments(*, object_store: Any, thread_id: UUID | str) -> int | None:
    """按 thread 清扫本地热段目录；返回删除个数，**能力缺失时返回 ``None``**。

    ``None`` 与 ``0`` 语义严格区分：``0`` = "有能力且确实没有残留"（合规可继续），
    ``None`` = "这个 store 不会删本机热段"——调用方必须据此**拒绝**把该 thread 标成
    已删除（与 I-6 "缺对象存储不落 tombstone" 同一原则），否则就是"没删数据却宣称删了"。
    """
    if not isinstance(object_store, HotSegmentRootDeleter):
        return None
    count = await object_store.delete_hot_thread(thread_id=thread_id)
    return int(count)


class LocalHotSegmentCache:
    """``{root}/threads/...`` 热缓存目录的清理能力（可挂到任意对象存储实现上）。

    远端对象存储（Kodo）不持有"对象 key → 本地路径"的完整视图，但 recorder 在 kodo
    模式下**照样**把热段写在本地。把这份能力抽成独立小对象后：

    - :class:`LocalRolloutObjectStore` 直接用它的根目录；
    - :class:`~backend.conversation.rollout.qiniu_kodo.QiniuKodoRolloutObjectStore` 由
      factory 挂一个实例，于是"factory 产出的 store 都能删自己写过的热段"（新发现 15）。
    """

    def __init__(self, *, root: str | Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    async def delete_hot_segment(self, *, key: str) -> None:
        delete_hot_segment_file(root=self._root, object_key=key)

    async def delete_hot_thread(self, *, thread_id: UUID | str) -> int:
        return delete_hot_thread_files(root=self._root, thread_id=thread_id)


class LocalRolloutObjectStore:
    """本地目录模拟对象存储（开发与 Phase 2/3 的默认实现）。"""

    def __init__(self, *, root: str | Path) -> None:
        #: 对象镜像根 ``{root}/objects``；热缓存段则直接落在 ``{root}/threads/...``
        #: （路径由 rollout/file_naming.segment_path 决定），因此需要同时持有 base。
        self._base = Path(root)
        self._root = self._base / _OBJECTS_DIRNAME

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

    async def delete_hot_segment(self, *, key: str) -> None:
        """删除与 ``key`` 同构的本地热缓存段文件（C-3：合规删除必须删正文）。

        热段写在 ``{root}/threads/YYYY/MM/DD/<thread>/<ordinal>-<seg>.jsonl``，与对象
        镜像同构；只删 ``objects/`` 会让用户原文留在磁盘上，而 tombstone 之后再没有
        任何工具能发现它。文件不存在视为成功（幂等），其它 OSError 照常抛出。
        """
        delete_hot_segment_file(root=self._base, object_key=key)

    async def delete_hot_thread(self, *, thread_id: UUID | str) -> int:
        """按 thread 清理本地热缓存段（未封存段没有 object key，只能按目录清扫）。

        返回删除个数；重跑删除命令是幂等的（没有文件时返回 0）。
        """
        return delete_hot_thread_files(root=self._base, thread_id=thread_id)

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
        #: 删除路径要求删除的热缓存段 key（测试替身不持有文件系统，只记录调用）
        self.hot_segments_deleted: list[str] = []
        #: 删除路径要求清扫热段目录的 thread（同上，只记录调用）
        self.hot_threads_swept: list[UUID] = []

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

    async def delete_hot_segment(self, *, key: str) -> None:
        """测试替身：没有真实文件系统，只记录调用，便于断言删除路径**确实**要求删热段。"""
        self.hot_segments_deleted.append(key)

    async def delete_hot_thread(self, *, thread_id: UUID | str) -> int:
        """测试替身：记录"按 thread 清扫热段"的调用（未封存段走这条路径）。"""
        normalized = normalize_thread_id(thread_id)
        self.hot_threads_swept.append(normalized)
        return 0

    async def presign_read(self, *, key: str, expires_seconds: int) -> str:
        return f"fake://{key}?expires={expires_seconds}"

    async def health_check(self) -> bool:
        return True


def remove_local_object_root(*, root: str | Path) -> None:
    """测试辅助：整体清理 Local 对象目录。"""
    shutil.rmtree(Path(root) / _OBJECTS_DIRNAME, ignore_errors=True)
