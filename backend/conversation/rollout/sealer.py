"""段封存与恢复（memory-rebuild §5.4 Phase 2）。

**封存事务顺序**（§5.4，顺序铁律）：

1. 找热段（本 turn 已存在的 ``open`` 行）；
2. 追加完成、逐行 flush，算出字节数、ordinal 范围、sha256；
3. **先上传对象**并确认拿到 object ref；
4. 再在**同一 PG 事务**内写 manifest（sealed）与 message pointer；
5. 事务成功后段即 ``sealed``；PG 事务失败时对象暂时是孤儿，由 reconcile 处理
   ——**绝不产生"manifest 指向不存在对象"**。

**崩溃恢复**（Phase 2 决策）：重新 claim 同一 turn 时先查该 turn 的 ``open`` 段；
本地文件仍在 → 续写（复用 segment_id 与 ordinal_start）；本地文件不在（换节点）→
把旧 ``open`` 段标 ``deleted`` 并新建段（依赖迁移 0008 的部分唯一索引）。

本模块是 rollout 包与 persistence 层之间唯一的桥：recorder 不直接写库，
repository 不直接碰文件与对象存储。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.contracts.object_store import RolloutObjectStore
from backend.conversation.persistence import rollout_manifests as manifests_repo
from backend.conversation.rollout.object_store import build_object_key


@dataclass(slots=True)
class MessagePointer:
    """一条消息在段内的位置（落 conversation_messages 的四个指针列）。"""

    message_id: UUID
    ordinal: int
    byte_offset_start: int
    byte_offset_end: int


@dataclass(slots=True)
class ResumeInfo:
    """可续写的未封存段。"""

    segment_id: UUID
    ordinal_start: int
    last_ordinal: int
    path: Path


@dataclass(slots=True)
class SealRequest:
    """一次封存所需的全部事实（由 recorder 采集，sealer 负责落库）。"""

    segment_id: UUID
    thread_id: UUID
    turn_id: UUID
    thread_created_at: datetime
    path: Path
    ordinal_start: int
    ordinal_end: int
    byte_size: int
    sha256: str
    message_pointers: list[MessagePointer] = field(default_factory=list)
    fence: tuple[str, int] | None = None


@dataclass(slots=True)
class SealResult:
    """封存结果；``sealed=False`` 表示未成功（已降级，不抛给调用方）。"""

    sealed: bool
    object_key: str | None = None
    reason: str | None = None


class RolloutSegmentSealer:
    """解析可续写段 + 按顺序铁律封存段。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        object_store: RolloutObjectStore,
        logger: logging.Logger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._object_store = object_store
        self._logger = logger or logging.getLogger("conversation.rollout.sealer")

    # ------------------------------------------------------------------
    # 恢复
    # ------------------------------------------------------------------

    async def resolve_resume(
        self,
        *,
        turn_id: UUID,
        thread_id: UUID,
        thread_created_at: datetime,
        segment_path_for: Any,
    ) -> ResumeInfo | None:
        """判断本 turn 是否有可续写的未封存段。

        ``segment_path_for(ordinal_start, segment_id) -> Path`` 由调用方提供（recorder
        持有 thread 创建时间与根目录），避免 sealer 重复拼路径。
        """
        async with self._session_factory() as session:
            row = await manifests_repo.get_open_by_turn(session, turn_id)
            if row is None:
                return None
            path: Path = segment_path_for(row["ordinal_start"], row["segment_id"])
            if not await asyncio.to_thread(path.is_file):
                # 换节点执行：本地热段不在，按 Phase 2 决策标废并重建
                async with session.begin():
                    await manifests_repo.mark_deleted(session, segment_id=row["segment_id"])
                self._logger.warning(
                    "rollout 未封存段本地文件缺失，标记废弃并重建: turn=%s segment=%s",
                    turn_id,
                    row["segment_id"],
                )
                return None
            last_ordinal = _read_last_ordinal_or_start(path, row["ordinal_start"])
            return ResumeInfo(
                segment_id=row["segment_id"],
                ordinal_start=int(row["ordinal_start"]),
                last_ordinal=last_ordinal,
                path=path,
            )

    async def register_open(
        self,
        *,
        segment_id: UUID,
        thread_id: UUID,
        turn_id: UUID,
        ordinal_start: int,
    ) -> None:
        """懒创建 ``open`` manifest 行（首次物化段文件时调用）。

        失败只告警：没有 manifest 行不影响段本身可写，reconcile 会按"已上传未登记"
        或"本地有段无登记"识别出来。
        """
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await manifests_repo.insert_open(
                        session,
                        segment_id=segment_id,
                        thread_id=thread_id,
                        turn_id=turn_id,
                        ordinal_start=ordinal_start,
                    )
        except Exception as exc:
            self._logger.warning("rollout open manifest 登记失败: %s", exc)

    # ------------------------------------------------------------------
    # 封存
    # ------------------------------------------------------------------

    async def seal(self, request: SealRequest) -> SealResult:
        """按 §5.4 顺序封存；失败降级返回 ``sealed=False``，不抛给调用方。"""
        object_key = build_object_key(
            thread_created_at=request.thread_created_at,
            thread_id=request.thread_id,
            ordinal_start=request.ordinal_start,
            segment_id=request.segment_id,
        )
        try:
            data = request.path.read_bytes()
        except OSError as exc:
            self._logger.warning("rollout 封存读取段失败: %s", exc)
            return SealResult(sealed=False, object_key=object_key, reason="segment_unreadable")

        # 以实际落盘内容为准重算 sha256/字节数：上传的与登记进 manifest 的必须是
        # 同一份字节，否则 manifest 的校验和对不上对象。
        actual_sha = hashlib.sha256(data).hexdigest()
        if actual_sha != request.sha256 or len(data) != request.byte_size:
            self._logger.warning(
                "rollout 段内容与采集事实不一致，按实际内容封存: segment=%s", request.segment_id
            )

        # 步骤 3：先上传（不可变 put；同 key 异内容会抛 hash 冲突）
        try:
            object_ref = await self._object_store.put_immutable(key=object_key, data=data)
        except Exception as exc:
            self._logger.warning("rollout 对象上传失败，段保持 open 待 reconcile: %s", exc)
            return SealResult(sealed=False, object_key=object_key, reason="upload_failed")

        # 步骤 4：同一事务内写 manifest + message pointer
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    sealed = await manifests_repo.seal(
                        session,
                        segment_id=request.segment_id,
                        object_key=object_ref.key,
                        object_etag=object_ref.etag,
                        sha256=object_ref.sha256,
                        byte_size=object_ref.size,
                        ordinal_end=request.ordinal_end,
                        fence=request.fence,
                    )
                    if not sealed:
                        # 已被他人封存 / 已删除 / fencing 失败：不要写指针，避免双写
                        return SealResult(
                            sealed=False, object_key=object_key, reason="not_transitionable"
                        )
                    await _write_message_pointers(session, request)
        except Exception as exc:
            # 对象已上传但 PG 失败：孤儿对象交给 reconcile，绝不留下悬空 manifest
            self._logger.warning("rollout manifest 写入失败（对象成为孤儿）: %s", exc)
            return SealResult(sealed=False, object_key=object_key, reason="manifest_write_failed")
        return SealResult(sealed=True, object_key=object_key)


async def _write_message_pointers(session: AsyncSession, request: SealRequest) -> None:
    """把消息 → 段坐标写进 conversation_messages 的四个指针列。

    四个指针列由 CHECK 约束保证"同真同假"，因此这里必须一次写全。
    """
    from sqlalchemy import text

    for pointer in request.message_pointers:
        await session.execute(
            text(
                "UPDATE conversation.conversation_messages "
                "SET segment_id = :segment_id, rollout_ordinal = :ordinal, "
                "    rollout_byte_offset_start = :start, rollout_byte_offset_end = :end "
                "WHERE message_id = :message_id "
                "  AND thread_id = :thread_id AND turn_id = :turn_id"
            ),
            {
                "segment_id": request.segment_id,
                "ordinal": pointer.ordinal,
                "start": pointer.byte_offset_start,
                "end": pointer.byte_offset_end,
                "message_id": pointer.message_id,
                "thread_id": request.thread_id,
                "turn_id": request.turn_id,
            },
        )


def _read_last_ordinal_or_start(path: Path, ordinal_start: int) -> int:
    """续写起点：段内最后一个完整行的 ordinal；读不到则回退到 ordinal_start。"""
    from backend.conversation.rollout.recorder import read_last_ordinal

    last = read_last_ordinal(path)
    return ordinal_start if last is None else last


__all__ = [
    "MessagePointer",
    "ResumeInfo",
    "RolloutSegmentSealer",
    "SealRequest",
    "SealResult",
]
