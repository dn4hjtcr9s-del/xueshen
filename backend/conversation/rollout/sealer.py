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
    #: 登记失败（约束冲突 / 连接不可用 / 失租等）：段没有 manifest 行，**必须停止记录**，
    #: 否则封存时对象已上传却更新 0 行 → 孤儿对象 + 指针落空。
    failed = "failed"
    #: 行已建，但**热段改名失败**且已补偿标废（review-2 新发现 4②）：DB 已经变了，
    #: 不能谎称"没有 manifest 行"，但本地文件也不在 manifest 期望的位置，同样必须停止
    #: 本 turn 的记录。与 ``failed`` 分开是为了让日志/指标能区分两种故障。
    rename_failed = "rename_failed"


@dataclass(slots=True)
class RegisterResult:
    """``register_open`` 的结构化结果（可判定失败 + 原因，供调用方与指标使用）。"""

    status: RegistrationStatus
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.status not in (RegistrationStatus.failed, RegistrationStatus.rename_failed)


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
        local_max_ordinal: Any = None,
    ) -> ResumeInfo | NewSegment | None:
        """判断本 turn 的段该"续写复用"还是"新建"。

        ``segment_path_for(ordinal_start, segment_id) -> Path`` 由调用方提供（recorder
        持有 thread 创建时间与根目录），避免 sealer 重复拼路径；``new_segment_id``
        是调用方为"本次新建"预生成的 id（恢复路径下它也可能被直接采用）。

        ``local_max_ordinal(thread_id) -> int | None`` 由调用方提供：该 thread **本地热段
        文件里真实写过的最大 ordinal**。新建段时必须把它和 manifest 的
        ``max(COALESCE(ordinal_end, ordinal_start))`` 取大（review-2 新发现 4①）：
        ``open`` 段的 ``ordinal_end`` 为 NULL，只按 DB 算会复用已写过的序号，
        出现 ``(0,3)`` 与 ``(1,2)`` 这种重叠范围，而没有任何 reconcile 类别能发现它。

        **本函数刻意不接收 fence**（review-2 新发现 17）：它只读、不改任何状态，
        真正的 fencing 落点是两个写路径——``register_open`` 标废旧段、
        ``discard_open`` 标废空段，二者现在都通过 ``manifests_repo.mark_deleted``
        的 ``EXISTS(lease)`` 守卫。留一个永不使用的参数只会让读者误以为恢复路径也被
        fence 过。

        返回：

        - :class:`ResumeInfo`：该 turn 的段本地文件仍在 → 沿用 ``ordinal_start``
          续写（新 ``segment_id``/新对象 key），旧行由调用方登记时标废；
        - :class:`NewSegment`：无可用本地段（从无段，或本地文件缺失需跨节点重建）
          → ``ordinal_start`` 由 manifest 与本地热段共同给出，不与任何既有段范围重叠；
        - ``None``：查询失败等异常情况（rollout 是旁路，调用方放弃本 turn 的记录）。
        """
        scanned = await _scan_local_max_ordinal(local_max_ordinal, thread_id)
        async with self._session_factory.begin() as session:
            row = await manifests_repo.get_active_by_turn(session, turn_id)
            if row is None:
                ordinal_start = _safe_next_ordinal(
                    await manifests_repo.next_ordinal_start(session, thread_id), scanned
                )
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
            # 新段用 manifest 与本地热段共同给出的序号，保证范围不重叠（0009 的部分唯一索引）。
            next_start = _safe_next_ordinal(
                await manifests_repo.next_ordinal_start(session, thread_id), scanned
            )
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
        fence: tuple[str, int] | None = None,
    ) -> RegisterResult:
        """登记新段的 ``open`` manifest 行（首次物化段文件时调用）。

        ``supersede_segment_id`` 非空时，在**同一事务**内把旧行（``open`` 或 ``sealed``）
        标 ``deleted``——否则新行会撞 ``uq_rollout_segment_turn_active``。
        ``fence`` 非空时该标废带租约守卫（review-2 新发现 17）：失租者不能 tombstone
        当前持有者的活动段（守卫不通过 ⇒ 标废 0 行 ⇒ 新行多半撞唯一约束 ⇒ 判失败）。

        ``rename_from``/``rename_to`` 非空时在**事务提交后**把热段文件改名，使本地路径
        与 manifest 的 ``(ordinal_start, segment_id)`` 同源（``put_immutable`` 不允许同 key
        异内容，因此续写必须换新 segment_id ⇒ 新路径）。

        **改名失败不再谎称"登记失败"**（review-2 新发现 4②）：DB 事务此时已经提交，
        ``status=failed`` 会误导调用方以为"没有 manifest 行"。改为返回
        :attr:`RegistrationStatus.rename_failed`，并在同一处做**补偿**——

        1. 把刚插入的行标 ``deleted``（manifest 与实际"没有可用段"的事实一致）；
        2. 尽力删掉改名源文件（它的旧行已是 tombstone，留着就是不可发现的残留）；
           连删除都失败时按 ERROR 记日志并打指标，路径信息进日志便于人工清理。

        补偿本身失败也不抛错：此时会留下 ``open`` 行 + 缺失的本地文件，正好被
        ``reconcile`` 的 ``open_local_missing`` 检出（可发现、可收敛）。

        **失败必须可判定**（I-1）：返回 :class:`RegisterResult`，``failed`` 表示该段
        **没有** manifest 行。调用方据此停止本 turn 的记录，避免"对象已上传但
        manifest 更新 0 行"的孤儿对象 + 指针落空。异常在这里收敛成返回值并打指标，
        不让 rollout 的故障冒泡打断 turn（§1.5 降级原则）。
        """
        try:
            async with self._session_factory.begin() as session:
                superseded = False
                if supersede_segment_id is not None:
                    superseded = await manifests_repo.mark_deleted(
                        session, segment_id=supersede_segment_id, fence=fence
                    )
                inserted = await manifests_repo.insert_open(
                    session,
                    segment_id=segment_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    ordinal_start=ordinal_start,
                )
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
        if supersede_segment_id is not None and not superseded:
            # 守卫不通过（段已 deleted 或是失租者）：不谎称失败，让新行去撞 turn 级唯一索引，
            # 由上面的 except 收敛成 failed。这里只留一条可观测日志。
            self._logger.warning(
                "rollout 旧段未标废（已 deleted 或租约不匹配）: turn=%s 旧段=%s",
                turn_id,
                supersede_segment_id,
            )
        if rename_from is not None and rename_to is not None and rename_from != rename_to:
            try:
                await asyncio.to_thread(_rename_segment_file, rename_from, rename_to)
            except OSError as exc:
                self._logger.error(
                    "rollout 热段改名失败，补偿标废新段并清理改名源: turn=%s segment=%s "
                    "from=%s to=%s err=%s",
                    turn_id,
                    segment_id,
                    rename_from,
                    rename_to,
                    exc,
                )
                await self._compensate_failed_rename(segment_id=segment_id, leftover=rename_from)
                _inc_counter("rollout_segment_registration_total", result="rename_failed")
                return RegisterResult(
                    status=RegistrationStatus.rename_failed, reason=type(exc).__name__
                )
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

    async def _compensate_failed_rename(self, *, segment_id: UUID, leftover: Path) -> None:
        """改名失败后的补偿：标废新行 + 删掉留在旧路径上的文件（review-2 新发现 4②）。

        两步都容错：任何一步失败都记 ERROR 日志并打指标，不向上抛——调用方已经要停止
        本 turn 的记录了，这里的目标只是"别留下不可发现的残留"，不能反过来把 turn 打挂。
        """
        try:
            async with self._session_factory.begin() as session:
                await manifests_repo.mark_deleted(session, segment_id=segment_id)
        except Exception as exc:
            self._logger.error(
                "rollout 改名补偿：标废新段失败（留 reconcile 收敛）: segment=%s err=%s",
                segment_id,
                exc,
            )
            _inc_counter("rollout_segment_rename_compensation_total", result="db_failed")
        try:
            await asyncio.to_thread(leftover.unlink, True)
        except OSError as exc:
            self._logger.error(
                "rollout 改名补偿：删除改名源失败（残留热文件，按路径人工清理）: %s err=%s",
                leftover,
                exc,
            )
            _inc_counter("rollout_segment_rename_compensation_total", result="unlink_failed")
            return
        _inc_counter("rollout_segment_rename_compensation_total", result="cleaned")

    async def discard_open(self, *, segment_id: UUID, fence: tuple[str, int] | None = None) -> bool:
        """标废一个"已登记但从未写入"的空段；返回是否真的改动了行。

        空段没有对象，留在 manifest 只会让 reconcile 报"本地热段缺失"。失败不抛错
        （rollout 是旁路），但会留下 ``open`` 行由 reconcile 兜底。

        ``fence`` 非空时带租约守卫（review-2 新发现 17）：失租的 worker 不得标废当前
        持有者的段——那会连带清空对方的消息指针。
        """
        try:
            async with self._session_factory.begin() as session:
                return await manifests_repo.mark_deleted(
                    session, segment_id=segment_id, fence=fence
                )
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


async def _scan_local_max_ordinal(scanner: Any, thread_id: UUID) -> int | None:
    """调用方提供的本地热段 ordinal 扫描（无扫描器 / 扫描失败都返回 None = 不参与取大）。"""
    if scanner is None:
        return None
    try:
        value = await asyncio.to_thread(scanner, thread_id)
    except Exception as exc:  # 扫描失败不能阻断恢复：DB 值仍是安全下界
        logging.getLogger("conversation.rollout.sealer").warning(
            "rollout 本地热段 ordinal 扫描失败（按 manifest 值继续）: %s", exc
        )
        return None
    return None if value is None else int(value)


def _safe_next_ordinal(db_next: int, scanned: int | None) -> int:
    """新建段的安全 ``ordinal_start``：manifest 与本地热段已写 ordinal 的下一个。

    review-2 新发现 4①：``open`` 段的 ``ordinal_end`` 为 NULL，DB 侧只能按
    ``ordinal_start`` 计数，于是"段里已写到 ordinal=2"时仍会给出起点 1，
    两段范围重叠且没有任何 reconcile 类别能发现。
    """
    if scanned is None:
        return db_next
    return max(db_next, scanned + 1)


def _rename_segment_file(source: Path, target: Path) -> None:
    """把热段改名到新段的路径（同目录、仅文件名不同）。

    幂等：目标已存在视为"已改过名"，直接返回（重放路径）。目标不存在而**源也不存在**时
    让 ``Path.replace`` 抛 ``FileNotFoundError``——manifest 说文件在、两边却都没有，这是
    真异常，必须走调用方的补偿路径，不能静默当成成功（那会留下 manifest 与本地文件的错位）。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
    source.replace(target)


def _read_last_ordinal_or_start(path: Path, ordinal_start: int) -> int:
    """续写起点：段内最后一个完整行的 ordinal；读不到则回退到 ordinal_start。"""
    from backend.conversation.rollout.recorder import read_last_ordinal

    last = read_last_ordinal(path)
    return ordinal_start if last is None else last


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
