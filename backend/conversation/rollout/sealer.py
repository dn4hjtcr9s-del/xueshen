"""段封存与恢复（memory-rebuild §5.4 Phase 2；I-1 重试/跨节点恢复修复）。

**封存事务顺序**（§5.4，顺序铁律）：

1. 找热段（本 turn 已存在的非删除段）；
2. 追加完成、逐行 flush，算出字节数、ordinal 范围、sha256；
3. **先上传对象**并确认拿到 object ref；
4. 再在**同一 PG 事务**内写 manifest（sealed）与 message pointer；
5. 事务成功后段即 ``sealed``；PG 事务失败时对象暂时是孤儿，由 reconcile 处理
   ——**绝不产生"manifest 指向不存在对象"**。

**一个 turn 最多一个非删除段**（``uq_rollout_segment_turn_active``）。因此崩溃重跑时
必须先把该 turn 已有的非删除段处理掉，才能让重试的段被登记：

- 本地热段文件在（同节点重试）→ **复用段的身份**：沿用原 ``ordinal_start`` 继续追加，
  但**换新的 ``segment_id``**，于是对象 key 也是新的。这不是洁癖：``put_immutable``
  对"同 key 异内容"必须抛 :class:`ObjectHashMismatchError`（§5.5 不可变语义），
  续写后字节必然不同，用旧 key 重新上传会直接被对象存储拒绝，封存永远推进不了。
  旧行由 :func:`supersede` 标 ``deleted``（保留审计与旧对象引用），新行继承其序号范围。
- 本地热段文件不在（跨节点恢复）→ 同样标废旧行（``open`` 或 ``sealed``）并新建段，
  新段的 ``ordinal_start`` 由 manifest 给出（所有行含 deleted 的最大末序号 + 1），
  不会与任何既有段的 ordinal 范围重叠——这正是 ``0009`` 把
  ``(thread_id, ordinal_start)`` 改成部分唯一索引的原因。

**登记必须可判定**：新建段在写第一行之前就要登记 ``open`` 行；登记失败 ⇒ 直接放弃本
turn 的记录。否则会走到"对象已上传、manifest ``UPDATE`` 更新 0 行"的孤儿对象 +
指针全部落空（I-1 的原症状）。

本模块是 rollout 包与 persistence 层之间唯一的桥：recorder 不直接写库，
repository 不直接碰文件与对象存储。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
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
    """可续写的段：本地热段文件仍在，沿用其 ``ordinal_start`` 与文件继续追加。

    ``segment_id`` 是本次要用的**新**段 id（换新 id 才有全新的对象 key——``put_immutable``
    不允许同 key 异内容）。因此在写第一行之前必须把热段文件改名成
    ``<ordinal_start>-<新 segment_id>.jsonl``：本地路径是按 ``(ordinal_start,
    segment_id)`` 拼的，路径与 manifest 不同源的话，下一次重试会"找不到本地文件"
    而把刚写好的段判成跨节点遗留（真实缺陷，已在集成测试固定）。
    """

    #: 上次尝试用的段行（即将被标废；仅用于日志与审计）。
    superseded_segment_id: UUID
    #: 本次要用的**新**段 id（全新对象 key）。
    segment_id: UUID
    ordinal_start: int
    last_ordinal: int
    #: 旧热段文件路径（改名源）。
    path: Path
    #: 新热段文件路径（改名的目标，登记事务提交后执行）。
    target_path: Path
    #: 新段的 ``ordinal_end`` 下界：上次封存的末序号。重试可能没有新增记录，
    #: 直接写更小的 ``ordinal_end`` 会违反 ``ordinal_end >= ordinal_start`` 的同族约束。
    sealed_ordinal_end: int | None = None


@dataclass(slots=True)
class NewSegment:
    """无可复用段时的新建指令。"""

    ordinal_start: int
    #: 需要标废的既有非删除段（跨节点：本地文件缺失；无则 None）。
    supersede_segment_id: UUID | None = None


class RegistrationStatus(StrEnum):
    """``register_open`` 的结果——调用方据此决定"能否继续记录并封存"。"""

    #: 本次真的插入了 open 行。
    inserted = "inserted"
    #: 行已存在且就是目标段（重放），可继续。
    already_open = "already_open"
    #: 登记失败（约束冲突 / 连接不可用等）：段没有 manifest 行，**必须停止记录**，
    #: 否则封存时对象已上传却更新 0 行 → 孤儿对象 + 指针落空。
    failed = "failed"


@dataclass(slots=True)
class RegisterResult:
    """``register_open`` 的结构化结果（可判定失败 + 原因，供调用方与指标使用）。"""

    status: RegistrationStatus
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status is not RegistrationStatus.failed


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
    #: 复用既有段的序号范围时，上次封存的末序号（``ordinal_end`` 只进不退）。
    ordinal_end_floor: int | None = None


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
    # 恢复 / 复用
    # ------------------------------------------------------------------

    async def resolve_resume(
        self,
        *,
        turn_id: UUID,
        thread_id: UUID,
        thread_created_at: datetime,
        segment_path_for: Any,
        new_segment_id: UUID,
        fence: tuple[str, int] | None = None,
    ) -> ResumeInfo | NewSegment | None:
        """判断本 turn 的段该"续写复用"还是"新建"。

        ``segment_path_for(ordinal_start, segment_id) -> Path`` 由调用方提供（recorder
        持有 thread 创建时间与根目录），避免 sealer 重复拼路径；``new_segment_id``
        是调用方为"本次新建"预生成的 id（恢复路径下它也可能被直接采用）。

        返回：

        - :class:`ResumeInfo`：该 turn 的段本地文件仍在 → 沿用 ``ordinal_start``
          续写（新 ``segment_id``/新对象 key），旧行由调用方登记时标废；
        - :class:`NewSegment`：无可用本地段（从无段，或本地文件缺失需跨节点重建）
          → ``ordinal_start`` 由 manifest 给出，不与任何既有段范围重叠；
        - ``None``：查询失败等异常情况（rollout 是旁路，调用方放弃本 turn 的记录）。
        """
        async with self._session_factory.begin() as session:
            row = await manifests_repo.get_active_by_turn(session, turn_id)
            if row is None:
                ordinal_start = await manifests_repo.next_ordinal_start(session, thread_id)
                return NewSegment(ordinal_start=ordinal_start)
            segment_id = row["segment_id"]
            ordinal_start = int(row["ordinal_start"])
            path: Path = segment_path_for(ordinal_start, segment_id)
            file_present = await asyncio.to_thread(path.is_file)
            if file_present:
                last_ordinal = _read_last_ordinal_or_start(path, ordinal_start)
                self._logger.warning(
                    "rollout 复用 turn 既有段（重试/续写）：turn=%s 旧段=%s 新段=%s "
                    "ordinal_start=%s last_ordinal=%s",
                    turn_id,
                    segment_id,
                    new_segment_id,
                    ordinal_start,
                    last_ordinal,
                )
                return ResumeInfo(
                    superseded_segment_id=segment_id,
                    segment_id=new_segment_id,
                    ordinal_start=ordinal_start,
                    last_ordinal=last_ordinal,
                    path=path,
                    target_path=segment_path_for(ordinal_start, new_segment_id),
                    sealed_ordinal_end=_as_optional_int(row.get("ordinal_end")),
                )
            # 本地文件不在：换节点执行。旧行（open 或 sealed）标废后重建，
            # 新段用 manifest 给的序号，保证范围不重叠（0009 的部分唯一索引）。
            next_start = await manifests_repo.next_ordinal_start(session, thread_id)
            self._logger.warning(
                "rollout 段本地文件缺失，标记废弃并重建: turn=%s 旧段=%s status=%s 新起点=%s",
                turn_id,
                segment_id,
                row.get("status"),
                next_start,
            )
            return NewSegment(ordinal_start=next_start, supersede_segment_id=segment_id)

    async def register_open(
        self,
        *,
        segment_id: UUID,
        thread_id: UUID,
        turn_id: UUID,
        ordinal_start: int,
        supersede_segment_id: UUID | None = None,
        rename_from: Path | None = None,
        rename_to: Path | None = None,
    ) -> RegisterResult:
        """登记新段的 ``open`` manifest 行（首次物化段文件时调用）。

        ``supersede_segment_id`` 非空时，在**同一事务**内把旧行（``open`` 或 ``sealed``）
        标 ``deleted``——否则新行会撞 ``uq_rollout_segment_turn_active``。

        ``rename_from``/``rename_to`` 非空时在**事务提交后**把热段文件改名，使本地路径
        与 manifest 的 ``(ordinal_start, segment_id)`` 同源；改名失败视为登记失败
        （调用方降级），不留下"manifest 指向不存在的热段文件"的错位。

        **失败必须可判定**（I-1）：返回 :class:`RegisterResult`，``status=failed`` 表示
        该段**没有** manifest 行。调用方据此停止本 turn 的记录，避免"对象已上传但
        manifest 更新 0 行"的孤儿对象 + 指针落空。异常在这里收敛成返回值并打指标，
        不让 rollout 的故障冒泡打断 turn（§1.5 降级原则）。
        """
        try:
            async with self._session_factory.begin() as session:
                superseded = False
                if supersede_segment_id is not None:
                    superseded = await manifests_repo.mark_deleted(
                        session, segment_id=supersede_segment_id
                    )
                inserted = await manifests_repo.insert_open(
                    session,
                    segment_id=segment_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    ordinal_start=ordinal_start,
                )
            if rename_from is not None and rename_to is not None and rename_from != rename_to:
                await asyncio.to_thread(_rename_segment_file, rename_from, rename_to)
        except Exception as exc:
            self._logger.warning(
                "rollout open manifest 登记失败（该段无 manifest 行，停止记录）: "
                "turn=%s segment=%s err=%s",
                turn_id,
                segment_id,
                exc,
            )
            _inc_counter("rollout_segment_registration_total", result="failed")
            return RegisterResult(status=RegistrationStatus.failed, reason=type(exc).__name__)
        if superseded:
            self._logger.warning(
                "rollout 旧段已标废（tombstone）: turn=%s 旧段=%s", turn_id, supersede_segment_id
            )
        if inserted:
            _inc_counter("rollout_segment_registration_total", result="inserted")
            return RegisterResult(status=RegistrationStatus.inserted)
        # ON CONFLICT (segment_id) DO NOTHING 命中：行已存在且就是本段 → 幂等成功
        _inc_counter("rollout_segment_registration_total", result="already_open")
        return RegisterResult(status=RegistrationStatus.already_open)

    async def discard_open(self, *, segment_id: UUID) -> bool:
        """标废一个"已登记但从未写入"的空段；返回是否真的改动了行。

        空段没有对象，留在 manifest 只会让 reconcile 报"本地热段缺失"。失败不抛错
        （rollout 是旁路），但会留下 ``open`` 行由 reconcile 兜底。
        """
        try:
            async with self._session_factory.begin() as session:
                return await manifests_repo.mark_deleted(session, segment_id=segment_id)
        except Exception as exc:
            self._logger.warning("rollout 空段标废失败（留给 reconcile）: %s", exc)
            return False

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

        # ordinal_end 只进不退：复用既有段的序号范围时，重试可能没有产生更多记录。
        ordinal_end = request.ordinal_end
        if request.ordinal_end_floor is not None:
            ordinal_end = max(ordinal_end, request.ordinal_end_floor)

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
                        ordinal_end=ordinal_end,
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

    四个指针列由 CHECK 约束保证"同真同假"，因此这里必须一次写全。重试复用段时
    这些指针会被重写到新段上，因此不存在"指向已标废段"的长期悬垂。
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


def _rename_segment_file(source: Path, target: Path) -> None:
    """把热段改名到新段的路径（同目录、仅文件名不同；目标已存在时保留原文件）。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    source.replace(target)


def _as_optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _inc_counter(name: str, **labels: str) -> None:
    """指标写入失败绝不能影响写入链路，因此整体兜底。"""
    try:
        from backend.conversation import metrics

        metric = getattr(metrics, name, None)
        if metric is None:
            return
        if labels:
            metric.labels(**labels).inc()
        else:
            metric.inc()
    except Exception:
        pass


__all__ = [
    "MessagePointer",
    "NewSegment",
    "RegisterResult",
    "RegistrationStatus",
    "ResumeInfo",
    "RolloutSegmentSealer",
    "SealRequest",
    "SealResult",
]
