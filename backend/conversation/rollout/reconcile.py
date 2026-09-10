"""Rollout 段一致性对账（memory-rebuild §5.4 Phase 2 / §5.5 Phase 3）。

短期记忆的事实源是 per-turn 的 JSONL 段（本地热段 + 对象存储），PostgreSQL 只保留
manifest（``conversation_rollout_segments``）与消息指针。旁路写入意味着"上传成功但
登记失败""换节点后本地热段消失""对象被篡改""指针写越界"这些窗口都会留下不一致，
因此需要一个**只读扫描 + 显式修复**的对账器：

- :meth:`RolloutReconciler.scan` 只发现、不修改任何状态（verify 与 CI 门禁用它）；
- :meth:`RolloutReconciler.repair` 只执行被明确授权的两种修复，且默认 dry-run。

五类不一致（kind 常量见模块级 ``KIND_*``）：

1. ``sealed_object_missing``：manifest 已是 ``sealed``，但对象存储里没有该 ``object_key``
   ——这就是**悬空 manifest**，读取路径必然失败，必须处置；
2. ``sealed_checksum_mismatch``：对象存在，但内容 sha256 与 manifest 记录的 ``sha256``
   不一致（对象被篡改或写坏）；
3. ``open_local_missing``：manifest 是 ``open``，但本地热段文件不存在——换节点后遗留的
   孤儿登记，续写会读出空段；
4. ``orphan_object``：``rollouts/`` 前缀下存在对象，但**没有任何 manifest 行引用它**
   （含 ``deleted`` tombstone 行）——即"已上传未登记"；
5. ``pointer_out_of_range``：``conversation_messages`` 的 rollout 指针越界：
   ``rollout_byte_offset_end`` 超过该段对象的字节数，或 ``rollout_ordinal`` 不在
   ``[ordinal_start, ordinal_end]`` 区间内。

**修复边界**（§5.5：不得用脚本批量删除未知对象）：

- 允许：对象**确认缺失**的 ``sealed`` 段 → ``deleted``；
- 允许：本地文件**确认缺失**的 ``open`` 段 → ``deleted``；
- 禁止：删除孤儿对象、改写被篡改的对象、自动改指针——这些只在报告里列出交人工处理
  （对象删除是不可逆的，误删合法正文的代价远高于留一个待清理对象）；
- ``dry_run=True`` 时**不产生任何 DB 或对象存储写操作**：构造说明文字后直接返回。

**失败不吞**：对象存储的 ``ObjectStoreError``（``ObjectNotFoundError`` 除外）与数据库
异常都记日志后向上抛。扫描拿不到可信结论时宁可让 ``verify-manifest`` 显式失败，也不能
报绿——绿色报告必须意味着"确实核对过"。
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.contracts.object_store import (
    ObjectNotFoundError,
    ObjectStat,
    RolloutObjectStore,
)
from backend.conversation.persistence import rollout_manifests as manifests_repo
from backend.conversation.rollout.codec import sha256_hex
from backend.conversation.rollout.file_naming import (
    find_segment_dir,
    list_segment_paths,
)
from backend.conversation.rollout.object_store import ROLLOUT_OBJECT_PREFIX

# ---------------------------------------------------------------------------
# finding 种类
# ---------------------------------------------------------------------------

#: ``sealed`` manifest 但对象不存在。
KIND_SEALED_OBJECT_MISSING = "sealed_object_missing"
#: ``sealed`` manifest 但对象内容 sha256 与 manifest 记录不符。
KIND_SEALED_CHECKSUM_MISMATCH = "sealed_checksum_mismatch"
#: ``open`` manifest 但本地热段文件不存在。
KIND_OPEN_LOCAL_MISSING = "open_local_missing"
#: 对象存储里有对象但没有任何 manifest 行引用。
KIND_ORPHAN_OBJECT = "orphan_object"
#: ``conversation_messages`` 的 rollout 指针越界。
KIND_POINTER_OUT_OF_RANGE = "pointer_out_of_range"

#: 全部 kind（``ReconcileReport.counts`` 用它保证零计数的种类也出现在结果里）。
ALL_FINDING_KINDS: tuple[str, ...] = (
    KIND_SEALED_OBJECT_MISSING,
    KIND_SEALED_CHECKSUM_MISMATCH,
    KIND_OPEN_LOCAL_MISSING,
    KIND_ORPHAN_OBJECT,
    KIND_POINTER_OUT_OF_RANGE,
)

#: 只报告、不自动修复的 kind（修复需要人工判断或不可逆操作）。
MANUAL_FINDING_KINDS: tuple[str, ...] = (
    KIND_SEALED_CHECKSUM_MISMATCH,
    KIND_ORPHAN_OBJECT,
    KIND_POINTER_OUT_OF_RANGE,
)

#: repair 返回列表里失败项的前缀；CLI 据此决定是否返回非零退出码。
FAILURE_PREFIX = "失败"

#: 引用集合按键分页读取的页大小。
_REFERENCE_PAGE_SIZE = 1000
#: 引用集合的读取上限：达到上限只告警（宁可少报孤儿，也不误报合法对象）。
_MAX_REFERENCE_KEYS = 100_000

#: 指针扫描 SQL：必须走参数绑定，且只取有指针的消息（§5.4 第 5 类不一致）。
_POINTER_SQL = """
    SELECT m.message_id AS message_id,
           m.segment_id AS segment_id,
           m.rollout_ordinal AS rollout_ordinal,
           m.rollout_byte_offset_start AS rollout_byte_offset_start,
           m.rollout_byte_offset_end AS rollout_byte_offset_end,
           s.thread_id AS thread_id,
           s.object_key AS object_key,
           s.ordinal_start AS ordinal_start,
           s.ordinal_end AS ordinal_end,
           s.byte_size AS byte_size,
           s.status AS segment_status
    FROM conversation.conversation_messages AS m
    LEFT JOIN conversation.conversation_rollout_segments AS s
           ON s.segment_id = m.segment_id
    WHERE m.segment_id IS NOT NULL
    ORDER BY m.segment_id, m.rollout_ordinal
    LIMIT :limit
"""


@dataclass(slots=True)
class ReconcileFinding:
    """一条不一致；``detail`` 是给运维看的中文说明，其余字段用于定位。"""

    kind: str
    segment_id: UUID | None = None
    thread_id: UUID | None = None
    object_key: str | None = None
    detail: str = ""
    message_id: UUID | None = None


@dataclass(slots=True)
class ReconcileReport:
    """一次扫描的结果；``findings`` 为空即"一致"。"""

    findings: list[ReconcileFinding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """是否未发现任何不一致。"""
        return not self.findings

    def count(self, kind: str) -> int:
        """某一类不一致的数量。"""
        return sum(1 for finding in self.findings if finding.kind == kind)

    def of_kind(self, kind: str) -> list[ReconcileFinding]:
        """某一类不一致的全部条目（保持扫描顺序）。"""
        return [finding for finding in self.findings if finding.kind == kind]

    def counts(self) -> dict[str, int]:
        """按 kind 计数；五种已知 kind 即使为 0 也会出现。"""
        counter = Counter(finding.kind for finding in self.findings)
        ordered = {kind: counter.get(kind, 0) for kind in ALL_FINDING_KINDS}
        for kind in sorted(set(counter) - set(ALL_FINDING_KINDS)):
            ordered[kind] = counter[kind]
        return ordered

    def summary(self) -> str:
        """一行中文摘要，供 CLI 与日志使用。"""
        if self.ok:
            return "未发现不一致"
        parts = [f"{kind}={count}" for kind, count in self.counts().items() if count]
        return f"共 {len(self.findings)} 项不一致（{'、'.join(parts)}）"


class RolloutReconciler:
    """manifest / 本地热段 / 对象存储 / 消息指针四方的对账器。"""

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
        self._logger = logger or logging.getLogger("conversation.rollout.reconcile")

    # ------------------------------------------------------------------
    # 只读扫描
    # ------------------------------------------------------------------

    async def scan(self, *, limit: int = 1000) -> ReconcileReport:
        """只读扫描五类不一致，返回报告（不修改任何状态）。

        ``limit`` 同时约束 manifest 行、消息指针行与对象列举的数量：对账是运维/CI
        入口，宁可分批多次跑，也不做无界全表扫描。
        """
        if limit < 1:
            raise ValueError("limit 必须为正整数")
        # 先把 PG 侧读干净再访问对象存储：不在持有数据库连接时做网络 I/O。
        async with self._session_factory() as session:
            segments = await manifests_repo.list_orphan_candidates(session, limit=limit)
            referenced_keys = await self._load_referenced_keys(session)
            pointer_rows = await self._load_pointer_rows(session, limit=limit)

        object_sizes: dict[str, int] = {}
        findings: list[ReconcileFinding] = []

        # 第 1、2 类：sealed 段必须指向存在且内容一致的对象。
        for row in segments:
            if str(row.get("status")) == "sealed":
                findings.extend(await self._check_sealed_object(row, object_sizes))

        # 第 3 类：open 段必须有本地热段文件可续写。
        local_cache: dict[UUID, dict[UUID, Path]] = {}
        for row in segments:
            if str(row.get("status")) == "open":
                finding = await self._check_open_local(row, local_cache)
                if finding is not None:
                    findings.append(finding)

        # 第 4 类：对象存储里存在但无人登记的对象。
        object_stats = await self._list_objects(limit=limit)
        for stat in object_stats:
            object_sizes.setdefault(stat.key, stat.size)
        findings.extend(self._find_orphan_objects(object_stats, referenced_keys))

        # 第 5 类：消息指针越界。
        findings.extend(self._check_pointers(pointer_rows, object_sizes))

        report = ReconcileReport(findings=findings)
        if report.ok:
            self._logger.info("rollout reconcile 扫描完成：%s", report.summary())
        else:
            self._logger.warning("rollout reconcile 扫描发现问题：%s", report.summary())
        return report

    async def _check_sealed_object(
        self, row: dict[str, Any], object_sizes: dict[str, int]
    ) -> list[ReconcileFinding]:
        """校验一个 ``sealed`` 段：对象存在（第 1 类）且内容 sha256 一致（第 2 类）。"""
        segment_id = _as_uuid(row.get("segment_id"))
        thread_id = _as_uuid(row.get("thread_id"))
        raw_key = row.get("object_key")
        if not raw_key:
            # ck_rollout_segment_sealed_fields 保证 sealed 必有 object_key；防御性上报。
            return [
                ReconcileFinding(
                    kind=KIND_SEALED_OBJECT_MISSING,
                    segment_id=segment_id,
                    thread_id=thread_id,
                    detail="sealed 段缺少 object_key，无法校验对象（manifest 行违反封存约束）",
                )
            ]
        object_key = str(raw_key)
        try:
            data = await self._object_store.get(key=object_key)
        except ObjectNotFoundError:
            return [
                ReconcileFinding(
                    kind=KIND_SEALED_OBJECT_MISSING,
                    segment_id=segment_id,
                    thread_id=thread_id,
                    object_key=object_key,
                    detail="manifest 已 sealed 但对象存储中不存在该对象（悬空 manifest）",
                )
            ]
        # 实际对象大小优先于 manifest.byte_size：列举被 limit 截断时指针校验才有着落。
        object_sizes[object_key] = len(data)
        expected = str(row.get("sha256") or "").strip().lower()
        actual = sha256_hex(data)
        if expected and actual != expected:
            return [
                ReconcileFinding(
                    kind=KIND_SEALED_CHECKSUM_MISMATCH,
                    segment_id=segment_id,
                    thread_id=thread_id,
                    object_key=object_key,
                    detail=(
                        f"对象内容 sha256={actual} 与 manifest sha256={expected} 不一致"
                        f"（对象 {len(data)} 字节），疑似被篡改或写坏"
                    ),
                )
            ]
        return []

    async def _check_open_local(
        self, row: dict[str, Any], cache: dict[UUID, dict[UUID, Path]]
    ) -> ReconcileFinding | None:
        """校验一个 ``open`` 段的本地热段文件是否仍在（第 3 类）。

        manifest 只有段创建时间，没有 thread 创建时间，而本地路径的日期目录取自
        **thread 创建时间**，因此这里用 :func:`find_segment_dir` 反查目录；同一 thread
        在一次扫描内只列举一次目录。
        """
        segment_id = _as_uuid(row.get("segment_id"))
        thread_id = _as_uuid(row.get("thread_id"))
        if segment_id is None or thread_id is None:
            return None
        index = cache.get(thread_id)
        if index is None:
            index = await asyncio.to_thread(
                _index_local_segments, root=self._root, thread_id=thread_id
            )
            cache[thread_id] = index
        if segment_id in index:
            return None
        return ReconcileFinding(
            kind=KIND_OPEN_LOCAL_MISSING,
            segment_id=segment_id,
            thread_id=thread_id,
            detail=(
                f"open 段在本地根目录 {self._root} 下找不到段文件"
                f"（ordinal_start={row.get('ordinal_start')}），疑似换节点后遗留的登记"
            ),
        )

    def _find_orphan_objects(
        self, object_stats: list[ObjectStat], referenced_keys: set[str]
    ) -> list[ReconcileFinding]:
        """第 4 类：``rollouts/`` 下有对象但没有任何 manifest 行引用。"""
        findings: list[ReconcileFinding] = []
        for stat in object_stats:
            if stat.key in referenced_keys:
                continue
            # 注意：deleted tombstone 行也算"引用"（key 仍在行里），因此这里只报
            # 真正无人登记的对象；tombstone 遗留对象由 retention/删除流程负责清理。
            findings.append(
                ReconcileFinding(
                    kind=KIND_ORPHAN_OBJECT,
                    object_key=stat.key,
                    detail=(
                        f"对象存储中存在对象（{stat.size} 字节）但没有任何 manifest 行引用，"
                        "应为「上传成功、登记失败」的孤儿对象；不在自动修复范围内，需人工确认后清理"
                    ),
                )
            )
        return findings

    def _check_pointers(
        self, pointer_rows: list[dict[str, Any]], object_sizes: dict[str, int]
    ) -> list[ReconcileFinding]:
        """第 5 类：``conversation_messages`` 的 rollout 指针越界。"""
        findings: list[ReconcileFinding] = []
        for row in pointer_rows:
            reasons = self._pointer_violations(row, object_sizes)
            if not reasons:
                continue
            raw_key = row.get("object_key")
            findings.append(
                ReconcileFinding(
                    kind=KIND_POINTER_OUT_OF_RANGE,
                    segment_id=_as_uuid(row.get("segment_id")),
                    thread_id=_as_uuid(row.get("thread_id")),
                    object_key=str(raw_key) if raw_key else None,
                    detail="；".join(reasons),
                    message_id=_as_uuid(row.get("message_id")),
                )
            )
        return findings

    def _pointer_violations(self, row: dict[str, Any], object_sizes: dict[str, int]) -> list[str]:
        """单个指针行违反的约束（可能同时命中多条）。"""
        if row.get("ordinal_start") is None:
            # FK 的 ON DELETE SET NULL 会先清空 segment_id，正常到不了这里。
            return ["指针指向的段没有 manifest 行，无法校验区间"]
        reasons: list[str] = []
        ordinal = _as_int(row.get("rollout_ordinal"))
        offset_end = _as_int(row.get("rollout_byte_offset_end"))
        ordinal_start = _as_int(row.get("ordinal_start"))
        ordinal_end = _as_int(row.get("ordinal_end"))
        raw_key = row.get("object_key")
        # 段对象字节数优先取实际对象；对象缺失时退回 manifest.byte_size（第 1 类已单独上报）。
        size = object_sizes.get(str(raw_key)) if raw_key else None
        if size is None:
            size = _as_int(row.get("byte_size"))
        if offset_end is not None and size is not None and offset_end > size:
            reasons.append(f"rollout_byte_offset_end={offset_end} 超过段对象字节数 {size}")
        if ordinal is not None and ordinal_start is not None and ordinal < ordinal_start:
            reasons.append(f"rollout_ordinal={ordinal} 小于 ordinal_start={ordinal_start}")
        if ordinal is not None and ordinal_end is not None and ordinal > ordinal_end:
            reasons.append(f"rollout_ordinal={ordinal} 大于 ordinal_end={ordinal_end}")
        return reasons

    async def _list_objects(self, *, limit: int) -> list[ObjectStat]:
        """列举 ``rollouts/`` 前缀下的对象；列举失败必须上抛（不能报绿）。"""
        return await self._object_store.list_prefix(prefix=ROLLOUT_OBJECT_PREFIX, limit=limit)

    # ------------------------------------------------------------------
    # 只读查询
    # ------------------------------------------------------------------

    async def _load_referenced_keys(self, session: AsyncSession) -> set[str]:
        """全部**非 deleted** manifest 行的 ``object_key``（有效引用集合）。

        刻意排除 ``deleted`` tombstone：tombstone 表示"这个对象本应已被物理删除"，
        把它算作有效引用会让"已删段的对象仍然存在"这类残留永远不被报为孤儿
        ——那是静默的数据泄漏。排除后这类残留会在 kind 4 里显式暴露出来。


        按键分页读取避免无界查询；达到 ``_MAX_REFERENCE_KEYS`` 时告警——引用集合不完整
        只会让判定偏保守方向的错误（把被引用的对象报成孤儿）风险上升，因此宁可告警。
        """
        keys: set[str] = set()
        after: str | None = None
        while True:
            params: dict[str, Any] = {"page": _REFERENCE_PAGE_SIZE}
            clause = ""
            if after is not None:
                clause = " AND object_key > :after"
                params["after"] = after
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT DISTINCT object_key "
                            "FROM conversation.conversation_rollout_segments "
                            "WHERE object_key IS NOT NULL AND status <> 'deleted'" + clause + " "
                            "ORDER BY object_key LIMIT :page"
                        ),
                        params,
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                break
            page_keys = [str(key) for key in rows]
            keys.update(page_keys)
            after = page_keys[-1]
            if len(page_keys) < _REFERENCE_PAGE_SIZE:
                break
            if len(keys) >= _MAX_REFERENCE_KEYS:
                self._logger.warning(
                    "rollout manifest 引用集合达到上限 %s，孤儿对象判定可能不完整",
                    _MAX_REFERENCE_KEYS,
                )
                break
        return keys

    async def _load_pointer_rows(
        self, session: AsyncSession, *, limit: int
    ) -> list[dict[str, Any]]:
        """带 rollout 指针的消息行 + 其段事实（全部参数绑定）。"""
        rows = (await session.execute(text(_POINTER_SQL), {"limit": limit})).mappings().all()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 修复
    # ------------------------------------------------------------------

    async def repair(self, report: ReconcileReport, *, dry_run: bool = True) -> list[str]:
        """执行允许的修复，返回人可读的操作说明列表。

        只处理两类可安全自动化的不一致：对象缺失的 ``sealed`` 段、本地文件缺失的
        ``open`` 段，动作都是 ``mark_deleted``（tombstone，保留审计）。``dry_run=True``
        时不触碰数据库与对象存储，只返回"将会做什么"。
        """
        targets = [
            finding
            for finding in report.findings
            if finding.segment_id is not None
            and finding.kind in (KIND_SEALED_OBJECT_MISSING, KIND_OPEN_LOCAL_MISSING)
        ]
        actions: list[str] = []
        if dry_run:
            self._logger.info("rollout reconcile 修复演练：%d 个段待标记 deleted", len(targets))
            for finding in targets:
                actions.append(
                    f"[dry-run] 将把{finding.kind}的段 {finding.segment_id} 标记为 deleted"
                )
        else:
            for finding in targets:
                actions.append(await self._mark_segment_deleted(finding))
        if report.count(KIND_ORPHAN_OBJECT):
            actions.append("不自动删除对象存储中的孤儿对象（不可逆），请人工确认后清理")
        for kind in MANUAL_FINDING_KINDS:
            count = report.count(kind)
            if count and kind != KIND_ORPHAN_OBJECT:
                actions.append(f"不自动修复（需人工处理）：{kind} × {count}")
        if dry_run and actions:
            # 报告为空时不返回任何说明，调用方可据此判断"什么都不用做"。
            actions.append("[dry-run] 未产生任何写操作；确认后加 --apply 执行")
        return actions

    async def _mark_segment_deleted(self, finding: ReconcileFinding) -> str:
        """把单个段标记为 ``deleted``；失败记日志并返回失败说明（不中断其余条目）。"""
        segment_id = finding.segment_id
        if segment_id is None:  # 调用方已过滤；仅为类型收窄
            return f"{FAILURE_PREFIX}：缺少 segment_id，无法标记 deleted"
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    updated = await manifests_repo.mark_deleted(session, segment_id=segment_id)
        except Exception as exc:
            self._logger.error(
                "rollout reconcile 标记段 deleted 失败: segment=%s kind=%s err=%s",
                segment_id,
                finding.kind,
                exc,
                exc_info=True,
            )
            return f"{FAILURE_PREFIX}：段 {segment_id} 未能标记 deleted：{exc}"
        if updated:
            self._logger.warning(
                "rollout reconcile 已把段标记 deleted: segment=%s kind=%s",
                segment_id,
                finding.kind,
            )
            return f"已把{finding.kind}的段 {segment_id} 标记为 deleted"
        return f"未变更：段 {segment_id} 已不是可删除状态（可能已被其他执行者处理）"


def _index_local_segments(*, root: str | Path, thread_id: UUID) -> dict[UUID, Path]:
    """同步列举该 thread 的本地段文件（ASYNC240：Path 操作不得出现在 async 函数里）。"""
    thread_dir = find_segment_dir(root=root, thread_id=thread_id)
    if thread_dir is None:
        return {}
    return {segment_id: path for _ordinal_start, segment_id, path in list_segment_paths(thread_dir)}


def _as_uuid(value: Any) -> UUID | None:
    """把驱动返回的 UUID/字符串统一成 :class:`UUID`；无法解析时返回 None。"""
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _as_int(value: Any) -> int | None:
    """把驱动返回的整数/NULL 统一成 ``int | None``。"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "ALL_FINDING_KINDS",
    "FAILURE_PREFIX",
    "KIND_OPEN_LOCAL_MISSING",
    "KIND_ORPHAN_OBJECT",
    "KIND_POINTER_OUT_OF_RANGE",
    "KIND_SEALED_CHECKSUM_MISMATCH",
    "KIND_SEALED_OBJECT_MISSING",
    "MANUAL_FINDING_KINDS",
    "ReconcileFinding",
    "ReconcileReport",
    "RolloutReconciler",
]
