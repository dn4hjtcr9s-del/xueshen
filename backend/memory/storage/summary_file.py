"""``memory_summary.md`` 的读写、版本快照与批次幂等凭证（memory-rebuild §2.3 / §5.9②）。

§2.3 规定 summary **无 front matter、首行恰好 `v1`**，因此它不能走
``memory_documents`` 的不可变版本机制（那会加 front matter 并占用一个 memory_id）。
用户 2026-09-12 裁决 A：保持纯文件格式，旧版本以**同目录快照**
``memory_summary.v{N}.md`` 保留，回滚 = 用快照覆盖回 ``memory_summary.md``。

另外写一份 ``memory_summary.meta.json``：``{version, batch_operation_id, checksum,
generated_at, degraded}``。它有两个作用：

1. 回滚与审计的可读凭证（哪一批、哪个版本、什么内容哈希）；
2. **末段幂等**（§5.9 验收"末段重试不重复写同一 summary 版本"）：同一批次重跑时，
   meta 里的 ``batch_operation_id`` 与当前批次一致即跳过，不再调 LLM、不再写新版本。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import UUID

#: 预注入摘要文件名（§2.3：位于用户目录根，不在 current/ 下）。
SUMMARY_FILENAME = "memory_summary.md"
#: 摘要元信息（版本 / 批次 / checksum）；prime 读取路径不读它，纯旁路。
SUMMARY_META_FILENAME = "memory_summary.meta.json"
#: 历史版本快照文件名模板。
SUMMARY_SNAPSHOT_TEMPLATE = "memory_summary.v{version}.md"
#: §2.3：首行必须恰好是该标记。
SUMMARY_SCHEMA_MARKER = "v1"

#: 保留的历史摘要版本数上限：超出后删除最旧的快照（磁盘有界）。
MAX_SUMMARY_SNAPSHOTS = 10


@dataclass(frozen=True)
class SummaryMeta:
    """``memory_summary.meta.json`` 的内容。"""

    version: int
    batch_operation_id: str | None
    checksum: str
    generated_at: datetime
    degraded: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "batch_operation_id": self.batch_operation_id,
            "checksum": self.checksum,
            "generated_at": self.generated_at.isoformat(),
            "degraded": self.degraded,
        }


def summary_dir(root: str | Path, user_id: UUID) -> Path:
    """用户目录（与 ``LocalMarkdownStore`` 的分片布局一致）。"""
    return Path(root) / "users" / str(user_id)[:2] / str(user_id)


def summary_path(root: str | Path, user_id: UUID) -> Path:
    return summary_dir(root, user_id) / SUMMARY_FILENAME


def summary_meta_path(root: str | Path, user_id: UUID) -> Path:
    return summary_dir(root, user_id) / SUMMARY_META_FILENAME


def summary_snapshot_path(root: str | Path, user_id: UUID, version: int) -> Path:
    return summary_dir(root, user_id) / SUMMARY_SNAPSHOT_TEMPLATE.format(version=version)


# ---------------------------------------------------------------------------
# 同步实现（ASYNC240：pathlib/os 调用不得出现在 async 函数里）
# ---------------------------------------------------------------------------


def read_summary_sync(root: str | Path, user_id: UUID) -> bytes | None:
    """读当前摘要原文；不存在或不可读返回 None。"""
    path = summary_path(root, user_id)
    try:
        return path.read_bytes()
    except OSError:
        return None


def read_summary_meta_sync(root: str | Path, user_id: UUID) -> SummaryMeta | None:
    """读元信息；缺失/损坏一律返回 None（按"没有元信息"处理，不影响摘要本身可用）。"""
    path = summary_meta_path(root, user_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        generated_at = datetime.fromisoformat(str(raw["generated_at"]))
    except (KeyError, ValueError):
        return None
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    return SummaryMeta(
        version=int(raw.get("version") or 0),
        batch_operation_id=(
            str(raw["batch_operation_id"]) if raw.get("batch_operation_id") else None
        ),
        checksum=str(raw.get("checksum") or ""),
        generated_at=generated_at,
        degraded=bool(raw.get("degraded")),
    )


def _atomic_write(path: Path, content: bytes) -> None:
    """与 ``LocalMarkdownStore`` 同款的原子写（临时文件 + fsync + rename）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_summary_sync(
    root: str | Path,
    user_id: UUID,
    body: str,
    *,
    batch_operation_id: UUID | None,
    generated_at: datetime,
    degraded: bool = False,
) -> SummaryMeta:
    """写新摘要：**先快照旧内容**，再原子替换 ``memory_summary.md``，最后写 meta。

    顺序有意为之：快照失败就不动当前摘要（宁可这次不写，也不能让"上一版回滚点"丢失）。
    内容与当前完全一致时**不升版本**（只更新 meta 的批次信息），避免重试把版本号刷爆。
    """
    directory = summary_dir(root, user_id)
    directory.mkdir(parents=True, exist_ok=True)
    previous = read_summary_meta_sync(root, user_id)
    current_bytes = read_summary_sync(root, user_id)
    content = _render_summary(body).encode("utf-8")
    checksum = sha256(content).hexdigest()

    if current_bytes is not None and sha256(current_bytes).hexdigest() == checksum:
        # 内容没变：不写新版本文件、不留重复快照，只把批次凭证推进到本批
        meta = SummaryMeta(
            version=previous.version if previous else 1,
            batch_operation_id=str(batch_operation_id) if batch_operation_id else None,
            checksum=checksum,
            generated_at=generated_at,
            degraded=degraded,
        )
        _atomic_write(summary_meta_path(root, user_id), _encode_meta(meta))
        return meta

    version = (previous.version + 1) if previous else 1
    if current_bytes is not None:
        # 旧内容留快照（回滚点）；放在替换之前
        _atomic_write(summary_snapshot_path(root, user_id, version - 1), current_bytes)
    _atomic_write(summary_path(root, user_id), content)
    meta = SummaryMeta(
        version=version,
        batch_operation_id=str(batch_operation_id) if batch_operation_id else None,
        checksum=checksum,
        generated_at=generated_at,
        degraded=degraded,
    )
    _atomic_write(summary_meta_path(root, user_id), _encode_meta(meta))
    _prune_snapshots(directory, keep=MAX_SUMMARY_SNAPSHOTS)
    return meta


def restore_summary_sync(root: str | Path, user_id: UUID, version: int) -> bool:
    """回滚到某个快照版本（§5.9②："旧 summary 可回滚"）。成功返回 True。"""
    snapshot = summary_snapshot_path(root, user_id, version)
    try:
        content = snapshot.read_bytes()
    except OSError:
        return False
    current = read_summary_sync(root, user_id)
    meta = read_summary_meta_sync(root, user_id)
    # 回滚前把"当前"也留一份快照，回滚本身也可逆
    if current is not None and meta is not None:
        _atomic_write(summary_snapshot_path(root, user_id, meta.version), current)
    _atomic_write(summary_path(root, user_id), content)
    _atomic_write(
        summary_meta_path(root, user_id),
        _encode_meta(
            SummaryMeta(
                version=version,
                batch_operation_id=None,
                checksum=sha256(content).hexdigest(),
                generated_at=datetime.now(UTC),
                degraded=False,
            )
        ),
    )
    return True


def _encode_meta(meta: SummaryMeta) -> bytes:
    return json.dumps(meta.to_json(), ensure_ascii=False, sort_keys=True).encode("utf-8")


def _prune_snapshots(directory: Path, *, keep: int) -> None:
    """只保留最近 keep 个快照；快照名里的版本号是单调递增的，按数字排序即可。"""
    snapshots: list[tuple[int, Path]] = []
    for path in directory.glob("memory_summary.v*.md"):
        stem = path.name.removeprefix("memory_summary.v").removesuffix(".md")
        if stem.isdigit():
            snapshots.append((int(stem), path))
    snapshots.sort()
    for _, path in snapshots[:-keep] if len(snapshots) > keep else []:
        try:
            path.unlink()
        except OSError:
            pass


def _render_summary(body: str) -> str:
    """拼上 §2.3 要求的首行标记；``body`` 是三个小节组成的正文。"""
    return f"{SUMMARY_SCHEMA_MARKER}\n{body.rstrip()}\n"


# ---------------------------------------------------------------------------
# 异步包装（节点侧调用这些）
# ---------------------------------------------------------------------------


async def read_summary(root: str | Path, user_id: UUID) -> bytes | None:
    return await asyncio.to_thread(read_summary_sync, root, user_id)


async def read_summary_meta(root: str | Path, user_id: UUID) -> SummaryMeta | None:
    return await asyncio.to_thread(read_summary_meta_sync, root, user_id)


async def write_summary(
    root: str | Path,
    user_id: UUID,
    body: str,
    *,
    batch_operation_id: UUID | None,
    generated_at: datetime | None = None,
    degraded: bool = False,
) -> SummaryMeta:
    return await asyncio.to_thread(
        write_summary_sync,
        root,
        user_id,
        body,
        batch_operation_id=batch_operation_id,
        generated_at=generated_at or datetime.now(UTC),
        degraded=degraded,
    )


async def restore_summary(root: str | Path, user_id: UUID, version: int) -> bool:
    return await asyncio.to_thread(restore_summary_sync, root, user_id, version)
