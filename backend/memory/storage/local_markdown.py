"""本地持久化卷 Markdown 存储实现（规格 §8.3 / §8.5 / §8.6）。

- versions/ 文件不可变；写入先临时文件后原子 rename。
- current/ 是物化副本，同样原子替换；失败不影响数据库活动指针。
- quarantine/ 仅用于 30 天恢复窗口。
- 路径只能由本模块生成：所有外部输入经过 memory_id 白名单校验。
"""

from __future__ import annotations

import asyncio
import os
import re
import tempfile
from pathlib import Path
from uuid import UUID

from backend.memory.contracts.common import validate_existing_topic_key
from backend.memory.storage.base import (
    StoredVersion,
    logical_path_for,
    sha256_hex,
    version_storage_key,
)

_SAFE_SEGMENT = re.compile(r"^[\w\-\u4e00-\u9fff]+$", re.UNICODE)


class StoragePathError(ValueError):
    """非法存储路径输入。"""


def _validate_memory_id(memory_id: str) -> None:
    if memory_id in ("learner", "index"):
        return
    if memory_id.startswith("mastery:"):
        from backend.memory.contracts.common import TopicKeyError

        try:
            validate_existing_topic_key(memory_id.removeprefix("mastery:"))
        except TopicKeyError as exc:
            raise StoragePathError(str(exc)) from exc
        return
    raise StoragePathError(f"非法 memory_id: {memory_id!r}")


def _validate_key_part(part: str) -> None:
    if not part or "/" in part or "\\" in part or part.startswith("."):
        raise StoragePathError(f"非法路径段: {part!r}")
    if not _SAFE_SEGMENT.match(part.split(".md")[0].replace("v", "v", 1)):
        # 允许版本文件名中的数字和连字符；仍拒绝路径穿越
        if ".." in part:
            raise StoragePathError(f"非法路径段: {part!r}")


class LocalMarkdownStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    # ---------------- 路径生成（唯一入口） ----------------

    def _user_dir(self, user_id: UUID) -> Path:
        shard = str(user_id)[:2]
        return self._root / "users" / shard / str(user_id)

    def _abs(self, user_id: UUID, relative: str) -> Path:
        base = self._user_dir(user_id)
        # storage_key 形如 users/{shard}/{user_id}/... 时剥离开头三段
        parts = relative.split("/")
        if parts[:3] == ["users", str(user_id)[:2], str(user_id)]:
            relative = "/".join(parts[3:])
        target = (base / relative).resolve()
        base_resolved = base.resolve()
        if base_resolved != target and base_resolved not in target.parents:
            raise StoragePathError(f"路径逃逸用户目录: {relative!r}")
        return target

    # ---------------- 原子写 ----------------

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(content)
                tmp.flush()
                os.fsync(tmp.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ---------------- 接口实现 ----------------

    async def write_immutable_version(
        self, *, user_id: UUID, memory_id: str, version: int, content: bytes
    ) -> StoredVersion:
        _validate_memory_id(memory_id)
        checksum = sha256_hex(content)
        key = version_storage_key(user_id, memory_id, version, checksum)
        path = self._abs(user_id, key)
        if path.exists():
            existing = await asyncio.to_thread(path.read_bytes)
            if sha256_hex(existing) != checksum:
                raise StoragePathError(f"不可变版本冲突: {key}")
        else:
            await asyncio.to_thread(self._atomic_write, path, content)
        return StoredVersion(storage_key=key, checksum=checksum, size_bytes=len(content))

    async def read_version(self, *, user_id: UUID, storage_key: str) -> bytes:
        for part in storage_key.split("/"):
            _validate_key_part(part)
        path = self._abs(user_id, storage_key)
        if not path.exists():
            raise FileNotFoundError(storage_key)
        return await asyncio.to_thread(path.read_bytes)

    async def read_version_by_id(
        self, *, user_id: UUID, memory_id: str, version: int, checksum: str
    ) -> bytes:
        key = version_storage_key(user_id, memory_id, version, checksum)
        return await self.read_version(user_id=user_id, storage_key=key)

    async def materialize_current(self, *, user_id: UUID, memory_id: str, content: bytes) -> None:
        _validate_memory_id(memory_id)
        path = self._abs(user_id, f"current/{logical_path_for(memory_id)}")
        await asyncio.to_thread(self._atomic_write, path, content)

    async def remove_current(self, *, user_id: UUID, memory_id: str) -> None:
        _validate_memory_id(memory_id)
        path = self._abs(user_id, f"current/{logical_path_for(memory_id)}")
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            pass

    async def move_to_quarantine(
        self, *, user_id: UUID, memory_id: str, deleted_version: int, deleted_at_epoch: int
    ) -> None:
        _validate_memory_id(memory_id)
        import hashlib

        memory_hash = hashlib.sha256(memory_id.encode()).hexdigest()[:16]
        quarantine_dir = self._abs(user_id, f"quarantine/{memory_hash}/delete-{deleted_at_epoch}")
        source_key = version_storage_key(user_id, memory_id, deleted_version, "0" * 64)
        # 按目录扫描定位实际版本文件（checksum 段未知时）
        version_dir = self._abs(user_id, source_key).parent
        prefix = f"v{deleted_version:08d}-"

        def _move() -> None:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            if version_dir.exists():
                for file in version_dir.iterdir():
                    if file.name.startswith(prefix):
                        target = quarantine_dir / file.name
                        if not target.exists():
                            file.rename(target)

        await asyncio.to_thread(_move)

    async def read_quarantined_version(
        self, *, user_id: UUID, memory_id: str, version: int, checksum: str
    ) -> bytes:
        _validate_memory_id(memory_id)
        import hashlib

        memory_hash = hashlib.sha256(memory_id.encode()).hexdigest()[:16]
        base = self._abs(user_id, f"quarantine/{memory_hash}")
        prefix = f"v{version:08d}-"
        suffix = f"-{checksum[:12]}.md"

        def _read() -> bytes:
            if not base.exists():
                raise FileNotFoundError(f"quarantine/{memory_hash}")
            for delete_dir in sorted(base.glob("delete-*"), reverse=True):
                if not delete_dir.is_dir():
                    continue
                for file in delete_dir.iterdir():
                    if file.name.startswith(prefix) and file.name.endswith(suffix):
                        return file.read_bytes()
            raise FileNotFoundError(f"{memory_id} v{version} 不在隔离区")

        return await asyncio.to_thread(_read)

    async def purge_quarantined(self, *, user_id: UUID, memory_id: str) -> None:
        import hashlib
        import shutil

        memory_hash = hashlib.sha256(memory_id.encode()).hexdigest()[:16]
        target = self._abs(user_id, f"quarantine/{memory_hash}")
        if target.exists():
            await asyncio.to_thread(shutil.rmtree, target)

    async def list_orphan_versions(
        self, *, user_id: UUID, memory_id: str, referenced_checksums: set[str]
    ) -> list[str]:
        _validate_memory_id(memory_id)
        orphans: list[str] = []
        if memory_id in ("learner", "index"):
            version_dir = self._abs(user_id, f"versions/{memory_id}/{memory_id}")
            prefix = f"versions/{memory_id}/{memory_id}"
        else:
            topic_key = memory_id.removeprefix("mastery:")
            version_dir = self._abs(user_id, f"versions/mastery/{topic_key}")
            prefix = f"versions/mastery/{topic_key}"

        def _scan() -> list[str]:
            found: list[str] = []
            if not version_dir.exists():
                return found
            for file in version_dir.iterdir():
                m = re.match(r"^v\d{8}-([0-9a-f]{12})\.md$", file.name)
                if m and m.group(1) not in {c[:12] for c in referenced_checksums}:
                    found.append(f"{prefix}/{file.name}")
            return sorted(found)

        orphans = await asyncio.to_thread(_scan)
        return orphans

    async def delete_version_file(self, *, user_id: UUID, storage_key: str) -> None:
        for part in storage_key.split("/"):
            _validate_key_part(part)
        path = self._abs(user_id, storage_key)
        try:
            await asyncio.to_thread(path.unlink)
        except FileNotFoundError:
            pass

    async def delete_user_tree(self, *, user_id: UUID) -> None:
        import shutil

        target = self._user_dir(user_id)
        if target.exists():
            await asyncio.to_thread(shutil.rmtree, target)
