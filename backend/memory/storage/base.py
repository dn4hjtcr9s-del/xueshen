"""Markdown 存储抽象（规格 §8.3 / §8.4 / §8.5）。

生产对象存储 Key 预留规则：
users/{shard}/{user_id}/versions/{memory_type}/{topic_key-or-fixed}/v{version:08d}-{checksum12}.md
第一版实现本地持久化卷（LocalMarkdownStore）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


@dataclass(frozen=True)
class StoredVersion:
    """已写入不可变版本区的版本描述。"""

    storage_key: str
    checksum: str
    size_bytes: int


def version_storage_key(user_id: UUID, memory_id: str, version: int, checksum: str) -> str:
    """对象存储 Key 规则（§8.4）；本地卷使用同一相对 Key。"""
    shard = str(user_id)[:2]
    if memory_id == "learner":
        memory_type, segment = "learner", "learner"
    elif memory_id == "index":
        memory_type, segment = "index", "index"
    elif memory_id.startswith("mastery:"):
        memory_type = "mastery"
        segment = memory_id.removeprefix("mastery:")
    else:
        raise ValueError(f"非法 memory_id: {memory_id}")
    return (
        f"users/{shard}/{user_id}/versions/{memory_type}/{segment}/"
        f"v{version:08d}-{checksum[:12]}.md"
    )


def logical_path_for(memory_id: str) -> str:
    """活动版本物化副本的逻辑路径（§13.3）。"""
    if memory_id in ("learner", "index"):
        return f"{memory_id}.md"
    if memory_id.startswith("mastery:"):
        return f"mastery/{memory_id.removeprefix('mastery:')}.md"
    raise ValueError(f"非法 memory_id: {memory_id}")


class MarkdownStore(Protocol):
    """MemoryService 唯一写入口使用的存储接口（§2.2）。"""

    async def write_immutable_version(
        self, *, user_id: UUID, memory_id: str, version: int, content: bytes
    ) -> StoredVersion:
        """写入不可变版本；同 key 已存在且内容一致则幂等返回。"""
        ...

    async def read_version(self, *, user_id: UUID, storage_key: str) -> bytes:
        """按数据库指针读取不可变版本（核心读取不读 current/）。"""
        ...

    async def read_version_by_id(
        self, *, user_id: UUID, memory_id: str, version: int, checksum: str
    ) -> bytes:
        """按 memory_id + version + checksum 定位版本（恢复路径，§8.7.3）。"""
        ...

    async def materialize_current(self, *, user_id: UUID, memory_id: str, content: bytes) -> None:
        """数据库提交后原子物化 current/ 副本；失败不影响活动版本。"""
        ...

    async def remove_current(self, *, user_id: UUID, memory_id: str) -> None:
        """删除记忆后移除物化副本。"""
        ...

    async def move_to_quarantine(
        self, *, user_id: UUID, memory_id: str, deleted_version: int, deleted_at_epoch: int
    ) -> None:
        """tombstone 期间把可恢复正文标记/移动到 quarantine/（§8.3）。"""
        ...

    async def read_quarantined_version(
        self, *, user_id: UUID, memory_id: str, version: int, checksum: str
    ) -> bytes:
        """从 quarantine/ 读取被隔离版本正文（恢复路径兜底，§8.7.3）。"""
        ...

    async def purge_quarantined(self, *, user_id: UUID, memory_id: str) -> None:
        """30 天到期后物理清理隔离正文（§2.3）。"""
        ...

    async def list_orphan_versions(
        self, *, user_id: UUID, memory_id: str, referenced_checksums: set[str]
    ) -> list[str]:
        """列出数据库未引用的孤立版本 key（24 小时后清理，§8.5）。"""
        ...

    async def delete_version_file(self, *, user_id: UUID, storage_key: str) -> None:
        """物理删除单个版本文件（账号删除/孤立清理）。"""
        ...

    async def delete_user_tree(self, *, user_id: UUID) -> None:
        """账号删除：物理删除该用户全部 Markdown（§21.3）。"""
        ...
