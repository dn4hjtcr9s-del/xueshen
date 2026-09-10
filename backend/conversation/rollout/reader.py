"""Rollout 段读取（memory-rebuild §5.4 Phase 2）。

读取顺序（§5.4「改造 source_read_service：rollout pointer → 本地 segment → 对象存储
→ 兼容 HTTP conversation reader」中的前两级由本模块负责）：**本地热缓存优先，未命中
再回对象存储**。Reader 只依赖 Phase 0 定好的 :class:`RolloutObjectStore` 协议，
对象存储 SDK 不会出现在这里。

**校验**：拿到字节后一律按 manifest 记录的 ``sha256`` 复核。不一致说明对象被篡改或
本地缓存串了内容，此时**宁可回退也不返回可疑正文**——正文是用户可见的回答来源，
静默返回坏数据比降级回 DB 更糟。

定位方式：manifest 的 ``object_key`` 与本地段路径同构，因此可直接反查本地文件，
不必依赖 thread 创建时间（manifest 只记段创建时间，跨零点会不同日）。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.contracts.object_store import (
    ObjectNotFoundError,
    ObjectStoreError,
    RolloutObjectStore,
)
from backend.conversation.contracts.rollout import RolloutContractError, RolloutRecord
from backend.conversation.persistence import rollout_manifests as manifests_repo
from backend.conversation.rollout.codec import iter_records
from backend.conversation.rollout.object_store import local_path_for_object_key


@dataclass(slots=True)
class SegmentBytes:
    """一次段内容获取的结果，带来源信息供指标与 read-repair 计数。"""

    data: bytes
    #: "local" | "object"
    source: str
    object_key: str
    sha256_verified: bool


def _is_usable(segment: dict[str, Any]) -> bool:
    """段是否可用于回读。

    **必须显式查 status**：迁移 0007 的清指针触发器只在**硬删除** manifest 行时触发，
    而 thread 删除与 retention 走的是 ``mark_deleted`` 软删——软删不会清
    conversation_messages 的四个指针列。若不在这里拦，"删除后的 message 永不从
    rollout 回读"（§5.4 验收）就只能寄希望于对象恰好已被物理删除，是脆弱的。
    """
    if str(segment.get("status")) == "deleted":
        return False
    return bool(segment.get("object_key"))


class RolloutReader:
    """按指针读取段与记录；任何不可用情形都返回 None 让调用方回退。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        object_store: RolloutObjectStore,
        rollout_root: str | Path,
        logger: logging.Logger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._object_store = object_store
        self._root = Path(rollout_root)
        self._logger = logger or logging.getLogger("conversation.rollout.reader")

    # ------------------------------------------------------------------
    # 段级读取
    # ------------------------------------------------------------------

    async def read_segment_bytes(
        self, *, object_key: str, expected_sha256: str | None
    ) -> SegmentBytes | None:
        """本地优先、对象兜底地取回段字节，并按需校验 sha256。"""
        local_path = local_path_for_object_key(root=self._root, object_key=object_key)
        data: bytes | None = None
        source = "local"
        if await asyncio.to_thread(local_path.is_file):
            try:
                data = await asyncio.to_thread(local_path.read_bytes)
            except OSError as exc:
                self._logger.warning("rollout 本地段读取失败，转对象存储: %s", exc)
                data = None
        if data is None:
            source = "object"
            try:
                data = await self._object_store.get(key=object_key)
            except ObjectNotFoundError:
                self._logger.info("rollout 段本地与对象存储均缺失: %s", object_key)
                return None
            except ObjectStoreError as exc:
                self._logger.warning("rollout 对象读取失败: %s", exc)
                return None
        verified = True
        if expected_sha256:
            verified = hashlib.sha256(data).hexdigest() == expected_sha256
            if not verified:
                # 宁可回退也不返回可疑正文
                self._logger.warning("rollout 段 sha256 与 manifest 不符，拒绝使用: %s", object_key)
                return None
        return SegmentBytes(
            data=data, source=source, object_key=object_key, sha256_verified=verified
        )

    async def read_segment(self, segment_id: UUID) -> SegmentBytes | None:
        """按 segment_id 读取整段（重放与导出用）。"""
        async with self._session_factory() as session:
            row = await manifests_repo.get_by_id(session, segment_id)
        if row is None or not _is_usable(row):
            return None
        return await self.read_segment_bytes(
            object_key=str(row["object_key"]), expected_sha256=row.get("sha256")
        )

    async def read_thread_records(self, thread_id: UUID) -> list[RolloutRecord]:
        """按 ordinal 顺序拼装该 thread 的全部记录（逻辑 thread 文件视图）。"""
        async with self._session_factory() as session:
            segments = await manifests_repo.list_by_thread(session, thread_id)
        records: list[RolloutRecord] = []
        for segment in segments:
            if not _is_usable(segment):
                continue
            payload = await self.read_segment_bytes(
                object_key=str(segment["object_key"]), expected_sha256=segment.get("sha256")
            )
            if payload is None:
                continue
            try:
                records.extend(iter_records(payload.data))
            except RolloutContractError as exc:
                self._logger.warning("rollout 段重放失败，跳过该段: %s", exc)
        records.sort(key=lambda record: record.ordinal)
        return records

    # ------------------------------------------------------------------
    # 记录级读取
    # ------------------------------------------------------------------

    async def read_message_content(self, *, message_row: dict[str, Any]) -> str | None:
        """按 message 行的指针读出正文；无指针/任何不可用时返回 None。

        返回 None 表示"回退到 conversation_messages.content"，由调用方决定并计数。
        """
        segment_id = message_row.get("segment_id")
        ordinal = message_row.get("rollout_ordinal")
        offset_start = message_row.get("rollout_byte_offset_start")
        offset_end = message_row.get("rollout_byte_offset_end")
        if segment_id is None or ordinal is None or offset_start is None or offset_end is None:
            return None
        async with self._session_factory() as session:
            segment = await manifests_repo.get_by_id(session, UUID(str(segment_id)))
        if segment is None or not _is_usable(segment):
            self._logger.info("rollout 指针指向的段不存在或已删除: %s", segment_id)
            return None
        payload = await self.read_segment_bytes(
            object_key=str(segment["object_key"]), expected_sha256=segment.get("sha256")
        )
        if payload is None:
            return None
        record = _record_at(
            payload.data, int(offset_start), int(offset_end), expected_ordinal=int(ordinal)
        )
        if record is None:
            self._logger.warning(
                "rollout 指针越界或记录 ordinal 不符: segment=%s ordinal=%s", segment_id, ordinal
            )
            return None
        content = record.payload.get("content")
        return content if isinstance(content, str) else None


def _record_at(
    data: bytes, offset_start: int, offset_end: int, *, expected_ordinal: int
) -> RolloutRecord | None:
    """按字节区间取出记录并复核 ordinal。

    区间必须落在文件内且恰好是一条完整行；ordinal 也要对得上——只校验区间会让
    "指针被写错但仍在文件内"这种错位悄悄返回**别的消息正文**，比读不到更危险。
    """
    if offset_start < 0 or offset_end <= offset_start or offset_end > len(data):
        return None
    chunk = data[offset_start:offset_end]
    if not chunk.endswith(b"\n"):
        return None
    try:
        record = next(iter(iter_records(chunk)))
    except (RolloutContractError, StopIteration):
        return None
    if record.ordinal != expected_ordinal:
        return None
    return record


__all__ = ["RolloutReader", "SegmentBytes"]
