"""Rollout 段文件命名与定位（memory-rebuild §1.5 / §5.5 Phase 1）。

本地段路径（Phase 1 决策，文档只给了 §1.5 的 thread 级逻辑名与 §5.5 的对象 key）::

    {root}/threads/YYYY/MM/DD/<thread_id>/<ordinal_start>-<segment_id>.jsonl

日期目录取 **thread 创建时间**（UTC），与 §1.5 照抄的 codex ``sessions/YYYY/MM/DD/``
分片一致，也与 §5.5 对象 key ``rollouts/threads/YYYY/MM/DD/<thread_id>/...`` 同构——
日期在 ``<thread_id>`` **之上**，意味着它是 thread 的属性；否则同一 thread 的段会散落
在多个日期目录里，路径就不再是"定位该 thread"的稳定地址。

**预留命名**：§1.5 为"回滚到第 N 轮"预留 ``rollout-<ts>-<thread_id>_<rollback_id>.jsonl``。
该形式**不是**正常段，:func:`parse_segment_filename` 对它返回 ``None``，避免被误当成段
参与 ordinal 计算。Phase 1 不产生这种文件。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

#: 根目录下的 thread 分片目录名（§1.5）。
THREADS_DIRNAME = "threads"

#: 段文件后缀。
SEGMENT_SUFFIX = ".jsonl"

#: 回滚段文件名前缀（§1.5 预留；Phase 1 不产生）。
ROLLBACK_FILE_PREFIX = "rollout-"


class SegmentNameError(ValueError):
    """段路径/文件名不合法。"""


def normalize_thread_id(thread_id: UUID | str) -> UUID:
    """校验并归一 thread_id；非法值直接拒绝，避免拼出可穿越的路径。"""
    try:
        return thread_id if isinstance(thread_id, UUID) else UUID(str(thread_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise SegmentNameError(f"非法 thread_id: {thread_id!r}") from exc


def segment_dir(*, root: str | Path, thread_created_at: datetime, thread_id: UUID | str) -> Path:
    """thread 的段目录 ``{root}/threads/YYYY/MM/DD/<thread_id>``。

    创建时间统一归一到 UTC 再取日期：本地时区会随时间漂移，跨时区部署时同一 thread
    可能落到两个日期目录。
    """
    if thread_created_at.tzinfo is None:
        raise SegmentNameError("thread_created_at 必须带时区")
    created = thread_created_at.astimezone(UTC)
    normalized = normalize_thread_id(thread_id)
    return (
        Path(root)
        / THREADS_DIRNAME
        / f"{created:%Y}"
        / f"{created:%m}"
        / f"{created:%d}"
        / str(normalized)
    )


def segment_filename(*, ordinal_start: int, segment_id: UUID | str) -> str:
    """段文件名 ``<ordinal_start>-<segment_id>.jsonl``。

    ``ordinal_start`` 前导补零，使字典序与数值序一致，便于目录列举与人工排查。
    """
    if ordinal_start < 0:
        raise SegmentNameError(f"ordinal_start 不得为负: {ordinal_start}")
    try:
        normalized = segment_id if isinstance(segment_id, UUID) else UUID(str(segment_id))
    except (ValueError, AttributeError, TypeError) as exc:
        raise SegmentNameError(f"非法 segment_id: {segment_id!r}") from exc
    return f"{ordinal_start:012d}-{normalized}{SEGMENT_SUFFIX}"


def segment_path(
    *,
    root: str | Path,
    thread_created_at: datetime,
    thread_id: UUID | str,
    ordinal_start: int,
    segment_id: UUID | str,
) -> Path:
    """段文件的完整路径。"""
    return segment_dir(
        root=root, thread_created_at=thread_created_at, thread_id=thread_id
    ) / segment_filename(ordinal_start=ordinal_start, segment_id=segment_id)


def parse_segment_filename(name: str) -> tuple[int, UUID] | None:
    """段文件名 → ``(ordinal_start, segment_id)``；非段文件返回 ``None``。

    返回 ``None`` 的情形（都**不是**错误，只是"不参与 ordinal 计算"）：
    回滚预留命名（§1.5）、临时文件（``.tmp`` 后缀）、以及任何不符合命名的文件。
    """
    if not name.endswith(SEGMENT_SUFFIX):
        return None
    if name.startswith(ROLLBACK_FILE_PREFIX):
        return None
    stem = name[: -len(SEGMENT_SUFFIX)]
    ordinal_part, separator, segment_part = stem.partition("-")
    if not separator or not ordinal_part.isdigit():
        return None
    try:
        return int(ordinal_part), UUID(segment_part)
    except (ValueError, AttributeError):
        return None


def list_segment_paths(thread_dir: Path) -> list[tuple[int, UUID, Path]]:
    """列出目录内的段，按 ``ordinal_start`` 升序。

    目录不存在时返回空列表（新 thread 的首次写入）。
    """
    if not thread_dir.is_dir():
        return []
    found: list[tuple[int, UUID, Path]] = []
    for entry in thread_dir.iterdir():
        if not entry.is_file():
            continue
        parsed = parse_segment_filename(entry.name)
        if parsed is None:
            continue
        ordinal_start, segment_id = parsed
        found.append((ordinal_start, segment_id, entry))
    found.sort(key=lambda item: item[0])
    return found


def iter_thread_dirs(*, root: str | Path, thread_id: UUID | str) -> list[Path]:
    """该 thread 在 ``{root}/threads/*/*/*/`` 下的**全部**日期分片目录（可能为空）。

    为什么需要"全部"：日期目录取的是**调用方传入**的 thread 创建时间。``runner`` 在
    thread 行还没落库时取的是 ``clock.now()``，与 ``conversation_threads.created_at``
    不保证同日；``find_segment_dir`` 只返回日期最新的那一个目录，会漏掉另一个分片。
    热段删除（review-2 新发现 6/15）与 ordinal 扫描都必须跨分片，因此这里返回列表。
    """
    base = Path(root) / THREADS_DIRNAME
    if not base.is_dir():
        return []
    normalized = str(normalize_thread_id(thread_id))
    return sorted(path for path in base.glob(f"*/*/*/{normalized}") if path.is_dir())


def max_local_ordinal(*, root: str | Path, thread_id: UUID | str) -> int | None:
    """该 thread 本地热段里**真实写过的最大 ordinal**；没有可读段时返回 ``None``。

    ``manifest`` 侧对 ``open`` 段只能给出 ``ordinal_start``（``ordinal_end`` 为 NULL，
    懒创建时不写），因此"下一个安全起点"必须结合本地文件（review-2 新发现 4①）。
    只读每个分片里 ``ordinal_start`` 最大的那个文件的尾部（ordinal 单调递增，
    最大值必然在最后一行），不做全文扫描。

    读不出最后一条完整行（空文件 / 半行 / 损坏）时返回 ``None``——此时 manifest 的
    ``COALESCE(ordinal_end, ordinal_start)`` 仍是安全下界，不夸大也不缩小起点。
    """
    from backend.conversation.rollout.recorder import read_last_ordinal

    best: int | None = None
    for thread_dir in iter_thread_dirs(root=root, thread_id=thread_id):
        segments = list_segment_paths(thread_dir)
        if not segments:
            continue
        _ordinal_start, _segment_id, last_path = segments[-1]
        last_ordinal = read_last_ordinal(last_path)
        if last_ordinal is None:
            continue
        best = last_ordinal if best is None else max(best, last_ordinal)
    return best


def find_segment_dir(*, root: str | Path, thread_id: UUID | str) -> Path | None:
    """在已有分片中反查 thread 的段目录（不知道创建时间时用）。

    按日期倒序扫描，找到即返回；仅用于离线工具与测试，写路径应直接用
    :func:`segment_dir`（调用方持有 thread 创建时间，无需扫描）。
    """
    normalized = normalize_thread_id(thread_id)
    base = Path(root) / THREADS_DIRNAME
    if not base.is_dir():
        return None
    for year in sorted((p for p in base.iterdir() if p.is_dir()), reverse=True):
        for month in sorted((p for p in year.iterdir() if p.is_dir()), reverse=True):
            for day in sorted((p for p in month.iterdir() if p.is_dir()), reverse=True):
                candidate = day / str(normalized)
                if candidate.is_dir():
                    return candidate
    return None
