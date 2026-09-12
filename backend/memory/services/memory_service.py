"""MemoryService：Markdown 唯一写入口与多文档原子提交（规格 §2.2 / §8.6 / §8.7）。

提交流程：
1. 事务外读取当前活动版本内容并应用 patch，渲染新版本；
2. 新版本先写入不可变 versions/（失败只留孤立版本，24 小时清理）；
3. 数据库事务：用户级 advisory lock → 按 memory_id 字典序锁文档 →
   mutation_id 重放检查 → 校验 expected_version → 写 commit、活动指针、
   检索索引、Outbox、index dirty；
4. 事务提交后原子物化 current/；失败不影响活动版本，维护任务可修复。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import (
    CommitMutationPlan,
    FrontMatterPatch,
    LearnerPatch,
    LearnerReplacement,
    MasteryPatch,
    MasteryReplacement,
)
from backend.memory.contracts.common import evidence_ref_hash
from backend.memory.contracts.errors import (
    InvalidPayloadError,
    LeaseFencedError,
    MemoryDeletedError,
    MemoryNotFoundError,
    MemoryRestoreExpiredError,
    MemoryVersionConflictError,
)
from backend.memory.contracts.operations import MutationResult
from backend.memory.persistence import commits as commits_repo
from backend.memory.persistence import documents as docs_repo
from backend.memory.persistence import operations as ops_repo
from backend.memory.persistence import outbox as outbox_repo
from backend.memory.persistence.database import acquire_user_lock
from backend.memory.storage.base import MarkdownStore, logical_path_for, sha256_hex
from backend.memory.storage.markdown_schema import (
    SCHEMA_VERSION_V2,
    IndexDocument,
    IndexEntry,
    LearnerDocument,
    MasteryDocument,
    extract_links,
    normalize_aliases,
    parse_index,
    parse_learner,
    parse_mastery,
    render_index,
    render_learner,
    render_mastery,
)
from backend.settings import Settings

MAX_PLANS_PER_OPERATION = 8


@dataclass
class CommitOutcome:
    mutations: list[MutationResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    replayed: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _validate_replay_consistency(
    existing: dict[str, Any], *, user_id: UUID, memory_id: str, action: str
) -> None:
    """mutation replay 一致性校验（评审 #5/#6）：原 commit 必须属于当前
    user/memory/action，否则是 mutation_id 误用，按非法 payload 拒绝。"""
    if (
        str(existing["user_id"]) != str(user_id)
        or existing["memory_id"] != memory_id
        or existing["action"] != action
    ):
        raise InvalidPayloadError(
            f"mutation_id 与原 commit 不一致: 期望 "
            f"user={user_id} memory={memory_id} action={action}，实际 "
            f"user={existing['user_id']} memory={existing['memory_id']} "
            f"action={existing['action']}"
        )


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _is_v2(doc: LearnerDocument | MasteryDocument) -> bool:
    """文档是否已升到 schema v2（只有 v2 才有 frontmatter ``name`` 可投影）。"""
    return int(getattr(doc, "schema_version", 0) or 0) >= SCHEMA_VERSION_V2


def _links_of_rendered(content: bytes) -> list[str]:
    """从**刚渲染出的**正文现算 ``[[link]]`` 目标（评审 I-11②）。

    为什么不读 ``doc.links``：那是上一次解析的产物，patch 路径（mastery /
    frontmatter）都不重算它，照抄会让投影滞后一个提交、首次写入甚至永远为空。
    正文是唯一事实源（§3.4），"这一版写了什么"当然以渲染结果为准。
    """
    return extract_links(content.decode("utf-8"))


# ---------------------------------------------------------------------------
# patch 应用（纯函数，确定性）
# ---------------------------------------------------------------------------


def apply_learner_patch(doc: LearnerDocument, patch: LearnerPatch) -> None:
    def apply(current: list[str], add: list[str], remove: list[str]) -> list[str]:
        remove_set = set(remove)
        return _dedupe_keep_order([x for x in current if x not in remove_set] + add)

    doc.preferences = apply(doc.preferences, patch.preferences_to_add, patch.preferences_to_remove)
    doc.goals = apply(doc.goals, patch.goals_to_add, patch.goals_to_remove)
    doc.plans = apply(doc.plans, patch.plans_to_add, patch.plans_to_remove)


def apply_mastery_patch(doc: MasteryDocument, patch: MasteryPatch) -> None:
    if patch.overview is not None:
        doc.overview = patch.overview
    doc.understood = _dedupe_keep_order(doc.understood + patch.understood_to_add)
    resolve_set = set(patch.difficulties_to_resolve)
    doc.difficulties = _dedupe_keep_order(
        [x for x in doc.difficulties if x not in resolve_set] + patch.difficulties_to_add
    )
    doc.review_advice = _dedupe_keep_order(doc.review_advice + patch.review_advice_to_add)
    doc.evidence_refs = _dedupe_keep_order(doc.evidence_refs + patch.evidence_refs_to_add)


def learner_from_replacement(base: LearnerDocument, replacement: LearnerReplacement) -> None:
    base.preferences = _dedupe_keep_order(replacement.preferences)
    base.goals = _dedupe_keep_order(replacement.goals)
    base.plans = _dedupe_keep_order(replacement.plans)


def mastery_from_replacement(base: MasteryDocument, replacement: MasteryReplacement) -> None:
    base.topic_title = replacement.topic_title
    base.overview = replacement.overview
    base.understood = _dedupe_keep_order(replacement.understood)
    base.difficulties = _dedupe_keep_order(replacement.difficulties)
    base.review_advice = _dedupe_keep_order(replacement.review_advice)
    base.evidence_refs = _dedupe_keep_order(replacement.evidence_refs)


def apply_frontmatter_patch(
    doc: LearnerDocument | MasteryDocument, patch: FrontMatterPatch
) -> None:
    """把 v2 frontmatter 补丁应用到文档（memory-rebuild §3.6①）。

    **只在 name 与 description 齐备时才把文档升到 schema v2**：v2 解析器要求这两个
    字段必填，半套 frontmatter 渲染成 v2 会让文档下一次读不出来。补丁不完整时保持
    原版本——宁可晚一版升级，也不能写出自己解析不了的文档。

    keywords 与 aliases 一样按"追加 + 去重保序"合并（§3.4：文档是唯一事实源，
    index 与 PG 索引列都只是它的投影）。
    """
    if patch.name:
        doc.name = patch.name
    if patch.description:
        doc.description = patch.description
    if patch.aliases:
        doc.aliases = normalize_aliases([*doc.aliases, *patch.aliases])
    if patch.keywords:
        doc.keywords = normalize_aliases([*doc.keywords, *patch.keywords])
    if doc.name and doc.description:
        doc.schema_version = max(doc.schema_version, SCHEMA_VERSION_V2)


def _changed_learner_sections(before: LearnerDocument, after: LearnerDocument) -> list[str]:
    sections: list[str] = []
    if before.preferences != after.preferences:
        sections.append("preferences")
    if before.goals != after.goals:
        sections.append("goals")
    if before.plans != after.plans:
        sections.append("plans")
    return sections


# ---------------------------------------------------------------------------
# MemoryService
# ---------------------------------------------------------------------------


class MemoryService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        store: MarkdownStore,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._store = store

    @property
    def store(self) -> MarkdownStore:
        """存储边界只读访问（维护分支使用，§10.7）。"""
        return self._store

    # ---------------- 读取 ----------------

    async def _load_active_document(
        self, session: AsyncSession, *, user_id: UUID, memory_id: str
    ) -> tuple[dict[str, Any], LearnerDocument | MasteryDocument] | None:
        row = await docs_repo.get_document(session, user_id=user_id, memory_id=memory_id)
        if row is None or row["deleted_at"] is not None or row["active_version"] is None:
            return None
        content = await self._store.read_version(
            user_id=user_id, storage_key=row["active_storage_key"]
        )
        text_content = content.decode("utf-8")
        if row["memory_type"] == "learner":
            return row, parse_learner(text_content)
        return row, parse_mastery(text_content)

    async def get_learner(self, *, user_id: UUID) -> LearnerDocument | None:
        async with self._session_factory() as session:
            loaded = await self._load_active_document(session, user_id=user_id, memory_id="learner")
            if loaded is None:
                return None
            _, doc = loaded
            assert isinstance(doc, LearnerDocument)
            return doc

    async def get_mastery(self, *, user_id: UUID, topic_key: str) -> MasteryDocument | None:
        async with self._session_factory() as session:
            loaded = await self._load_active_document(
                session, user_id=user_id, memory_id=f"mastery:{topic_key}"
            )
            if loaded is None:
                return None
            _, doc = loaded
            assert isinstance(doc, MasteryDocument)
            return doc

    async def get_index(self, *, user_id: UUID) -> tuple[IndexDocument | None, bool]:
        """返回 (index 文档或 None, stale)。未构建返回 (None, True)（§8.6.1）。"""
        async with self._session_factory() as session:
            row = await docs_repo.get_document(session, user_id=user_id, memory_id="index")
            if row is None or row["active_version"] is None:
                return None, True
            content = await self._store.read_version(
                user_id=user_id, storage_key=row["active_storage_key"]
            )
            doc = parse_index(content.decode("utf-8"))
            return doc, row["index_dirty_at"] is not None

    async def read_active_content(
        self, *, user_id: UUID, memory_id: str
    ) -> tuple[int, str, str] | None:
        """按活动版本读取原始正文（memory-rebuild §2.4 D3② / §5.7 Phase 5）。

        与 get_learner / get_mastery 共用同一删除抑制语义：文档行缺失、已 tombstone
        （deleted_at 非空）或没有活动版本一律返回 None——已 quarantined 的正文因此
        永远读不到。读取只走 DB 活动指针指向的 versions/ 不可变版本，
        **不读 current/ 物化副本**（§2.4 D3：版本化读取 + 删除抑制）。

        返回 (version, checksum, content)；checksum 是整篇正文的 SHA-256，
        供 memory.read 的引用溯源使用。
        """
        async with self._session_factory() as session:
            row = await docs_repo.get_document(session, user_id=user_id, memory_id=memory_id)
            if row is None or row["deleted_at"] is not None or row["active_version"] is None:
                return None
            content = await self._store.read_version(
                user_id=user_id, storage_key=row["active_storage_key"]
            )
        return int(row["active_version"]), sha256_hex(content), content.decode("utf-8")

    # ---------------- 内容组装 ----------------

    async def _build_new_content(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        plan: CommitMutationPlan,
        now: datetime,
    ) -> tuple[bytes, int | None, int, str | None, dict[str, Any]]:
        """返回 (内容, before_version, after_version, topic_key, 索引数据)。"""
        loaded = await self._load_active_document(
            session, user_id=user_id, memory_id=plan.memory_id
        )
        before_version: int | None = None
        if loaded is not None:
            row, current_doc = loaded
            before_version = int(row["active_version"])
        else:
            current_doc = None

        if plan.target_memory_type == "learner":
            base: LearnerDocument
            if isinstance(current_doc, LearnerDocument):
                base = current_doc
            else:
                base = LearnerDocument(user_id=user_id, version=0, updated_at=now)
            before_snapshot = LearnerDocument(
                user_id=user_id,
                version=base.version,
                updated_at=base.updated_at,
                preferences=list(base.preferences),
                goals=list(base.goals),
                plans=list(base.plans),
                evidence_refs=list(base.evidence_refs),
                confidence=base.confidence,
            )
            if plan.learner_patch is not None:
                apply_learner_patch(base, plan.learner_patch)
            if plan.frontmatter_patch is not None:
                apply_frontmatter_patch(base, plan.frontmatter_patch)
            if plan.replacement is not None:
                assert isinstance(plan.replacement, LearnerReplacement)
                learner_from_replacement(base, plan.replacement)
            after_version = (before_version or 0) + 1
            base.version = after_version
            base.updated_at = now
            content = render_learner(base).encode("utf-8")
            changed = _changed_learner_sections(before_snapshot, base)
            index_data: dict[str, Any] = {
                "title": "学习者档案",
                "summary": "；".join((base.goals or base.preferences or ["学习者档案"])[:3]),
                # §3.4 + 2026-09-12 裁决 A：keywords 是 v2 frontmatter 字段，文档是唯一
                # 事实源；这里只做投影，不写死空列表（生产者是 Phase 7 consolidation 节点）。
                "keywords": list(base.keywords),
                # §3.4：alias 是检索键、[[link]] 目标是路由依据，都要进投影与 search_text
                "aliases": list(base.aliases),
                # 评审 I-11②：links 必须从**新渲染正文**现算。`base.links` 是上一版解析
                # 结果（apply_learner_patch / apply_frontmatter_patch 都不重算它），照抄
                # 会让"首次写入含 [[链接]] 的正文"投影为空、此后永远滞后一个提交。
                "related_topic_keys": _links_of_rendered(content),
                "search_text": " ".join(
                    [
                        "学习者档案",
                        *base.aliases,
                        *base.preferences,
                        *base.goals,
                        *base.plans,
                    ]
                ),
                "changed_sections": changed,
            }
            return content, before_version, after_version, None, index_data

        # mastery
        topic_key = plan.memory_id.removeprefix("mastery:")
        if isinstance(current_doc, MasteryDocument):
            mbase = current_doc
        else:
            title = plan.topic_title or topic_key
            mbase = MasteryDocument(
                user_id=user_id,
                topic_key=topic_key,
                topic_title=title,
                version=0,
                updated_at=now,
            )
        if plan.topic_title:
            mbase.topic_title = plan.topic_title
        if plan.mastery_patch is not None:
            apply_mastery_patch(mbase, plan.mastery_patch)
        if plan.frontmatter_patch is not None:
            apply_frontmatter_patch(mbase, plan.frontmatter_patch)
        if plan.replacement is not None:
            assert isinstance(plan.replacement, MasteryReplacement)
            mastery_from_replacement(mbase, plan.replacement)
        after_version = (before_version or 0) + 1
        mbase.version = after_version
        mbase.updated_at = now
        content = render_mastery(mbase).encode("utf-8")
        # 评审 I-11③：v2 文档的注册表 title 取 frontmatter `name`（v2 里它就是文档标题），
        # v1 文档没有 name，保持 topic_title 逐字不变——否则改 name 的 frontmatter_patch
        # 永远到不了注册表与 search。
        projected_title = (mbase.name or mbase.topic_title) if _is_v2(mbase) else mbase.topic_title
        index_data = {
            "title": projected_title,
            "summary": mbase.overview or "；".join(mbase.understood[:3]),
            # 同 learner：keywords 由文档投影，文档是唯一事实源（§3.4 / 裁决 A）
            "keywords": list(mbase.keywords),
            "aliases": list(mbase.aliases),
            # related_topic_keys 同 learner，见上面的 I-11② 说明
            "related_topic_keys": _links_of_rendered(content),
            "search_text": " ".join(
                [
                    mbase.topic_title,
                    *mbase.aliases,
                    mbase.overview,
                    *mbase.understood,
                    *mbase.difficulties,
                    *mbase.review_advice,
                ]
            ),
        }
        return content, before_version, after_version, topic_key, index_data

    # ---------------- 原子提交 ----------------

    async def _mark_commit_started(
        self,
        operation_id: UUID,
        *,
        expected_worker: str | None = None,
        expected_generation: int | None = None,
        fencing_operation_id: UUID | None = None,
    ) -> None:
        """§11.6 + 评审二轮 #3：独立短事务打 commit 标记（fencing CAS）。

        携带 fencing token 且 CAS 失败说明 Lease 已易主：抛 LeaseFencedError，
        调用方（执行层）必须终止该旧执行者，不得进入业务提交路径。

        ``fencing_operation_id``（评审 I/C-2）是**真正持有 Lease 的 operation**：
        批量总结的成员 operation 从来没有被 claim（状态 ``pending_batch``、
        ``locked_by`` 为空），CAS 打在成员行上必然 0 行。缺省等于 ``operation_id``，
        即单条路径行为逐字不变。
        """
        cas_operation_id = fencing_operation_id or operation_id
        async with self._session_factory() as session:
            async with session.begin():
                ok = await ops_repo.mark_commit_started(
                    session,
                    operation_id=cas_operation_id,
                    expected_worker=expected_worker,
                    expected_generation=expected_generation,
                )
        if not ok and expected_worker is not None:
            raise LeaseFencedError(
                f"operation {cas_operation_id} commit 标记 CAS 失败（Lease 已易主）"
            )

    async def _clear_commit_started(
        self,
        operation_id: UUID,
        *,
        expected_worker: str | None = None,
        expected_generation: int | None = None,
        fencing_operation_id: UUID | None = None,
    ) -> None:
        """§11.6：独立短事务清除 commit 标记（提交事务结束后调用）。

        fencing CAS 失败仅说明 Lease 已易主：标记由新持有者负责，静默忽略。
        ``fencing_operation_id`` 语义同 :meth:`_mark_commit_started`。
        """
        async with self._session_factory() as session:
            async with session.begin():
                await ops_repo.clear_commit_started(
                    session,
                    operation_id=fencing_operation_id or operation_id,
                    expected_worker=expected_worker,
                    expected_generation=expected_generation,
                )

    async def commit_plans(
        self,
        *,
        operation_id: UUID,
        user_id: UUID,
        actor_type: str,
        plans: list[CommitMutationPlan],
        evidence_refs_by_plan: list[list[str]] | None = None,
        prompt_version: str | None = None,
        model_name: str | None = None,
        graph_node_ids_by_plan: list[list[str]] | None = None,
        mapping_methods_by_plan: list[str | None] | None = None,
        mapping_confidences_by_plan: list[float | None] | None = None,
        expected_worker: str | None = None,
        expected_generation: int | None = None,
        fencing_operation_id: UUID | None = None,
    ) -> CommitOutcome:
        """多文档原子提交（§8.6）。任何校验失败整个事务回滚。

        评审二轮 #3：经 Lease 领取的执行路径必须携带 expected_worker /
        expected_generation（fencing token）；CAS 失败抛 LeaseFencedError，
        业务副作用不发生。直调路径（测试/内部维护）可不携带，保持原语义。

        ``fencing_operation_id``（评审 C-2）：CAS 打在**真正持有 Lease 的
        operation** 上，缺省等于 ``operation_id``。批量总结里每个成员的提交都由
        批次 operation 的 Lease 驱动（成员自己从未被 claim），因此成员路径必须传
        批次 id；而 mutation 重放键、``memory_commits.operation_id`` 与 evidence
        绑定**始终**用成员 ``operation_id``，保证"哪条证据写成"可追溯、可重放。
        """
        if len(plans) > MAX_PLANS_PER_OPERATION:
            raise ValueError(f"一个 operation 最多 {MAX_PLANS_PER_OPERATION} 个 CommitMutationPlan")
        cas_operation_id = fencing_operation_id or operation_id
        now = _now()
        outcome = CommitOutcome()

        # 0. mutation replay 预检（评审 #5）：渲染和任何存储副作用之前查询全部
        #    mutation；命中则完全跳过文件写入，并验证原 commit 的 user/memory/action
        replayed: dict[UUID, dict[str, Any]] = {}
        async with self._session_factory() as session:
            async with session.begin():
                await acquire_user_lock(session, user_id)
                for plan in plans:
                    existing = await commits_repo.get_by_mutation_id(session, plan.mutation_id)
                    if existing is not None:
                        _validate_replay_consistency(
                            existing,
                            user_id=user_id,
                            memory_id=plan.memory_id,
                            action=plan.action,
                        )
                        replayed[plan.mutation_id] = existing
        pending: list[tuple[int, CommitMutationPlan]] = [
            (i, plan) for i, plan in enumerate(plans) if plan.mutation_id not in replayed
        ]
        results_by_index: dict[int, MutationResult] = {}
        for i, plan in enumerate(plans):
            existing = replayed.get(plan.mutation_id)
            if existing is not None:
                results_by_index[i] = MutationResult(
                    mutation_id=plan.mutation_id,
                    memory_id=plan.memory_id,
                    action=existing["action"],
                    before_version=existing["before_version"],
                    after_version=existing["after_version"],
                )
        if replayed:
            outcome.replayed = True

        # 1. 事务外渲染内容并写不可变版本（仅未提交过的 mutation；tuple 首位是
        #    原 plans 下标，用于对齐 evidence_refs/graph_node_ids 等按位参数）
        rendered: list[
            tuple[int, CommitMutationPlan, bytes, int | None, int, str | None, dict[str, Any]]
        ] = []
        async with self._session_factory() as session:
            for i, plan in pending:
                rendered.append(
                    (
                        i,
                        plan,
                        *await self._build_new_content(
                            session, user_id=user_id, plan=plan, now=now
                        ),
                    )
                )
        stored_by_index: dict[int, Any] = {}
        for i, plan, content, _bv, av, _tk, _idx in rendered:
            stored = await self._store.write_immutable_version(
                user_id=user_id, memory_id=plan.memory_id, version=av, content=content
            )
            stored_by_index[i] = stored

        # 2. 数据库事务
        # §11.6（裁决 2026-08-11）：进入 commit 副作用前用独立短事务打标记，
        # 取消仲裁据此返回 409；事务结束（含回滚）后清除，崩溃残留由执行层清理。
        # 标记同样打在 fencing operation 上：取消仲裁要拦的正是"持有 Lease 的那一行"。
        await self._mark_commit_started(
            operation_id,
            expected_worker=expected_worker,
            expected_generation=expected_generation,
            fencing_operation_id=cas_operation_id,
        )
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    await acquire_user_lock(session, user_id)
                    memory_ids = sorted({p.memory_id for p in plans})
                    locked_docs = await docs_repo.lock_documents(
                        session, user_id=user_id, memory_ids=memory_ids
                    )
                    docs_by_id = {d["memory_id"]: d for d in locked_docs}

                    for (
                        i,
                        plan,
                        _content,
                        before_version,
                        after_version,
                        topic_key,
                        index_data,
                    ) in rendered:
                        stored = stored_by_index[i]
                        # mutation_id 重放竞态防御（评审 #5）：预检后、本事务前
                        # 恰好有并发提交同一 mutation 时，直接复用原 commit
                        existing = await commits_repo.get_by_mutation_id(session, plan.mutation_id)
                        if existing is not None:
                            _validate_replay_consistency(
                                existing,
                                user_id=user_id,
                                memory_id=plan.memory_id,
                                action=plan.action,
                            )
                            results_by_index[i] = MutationResult(
                                mutation_id=plan.mutation_id,
                                memory_id=plan.memory_id,
                                action=existing["action"],
                                before_version=existing["before_version"],
                                after_version=existing["after_version"],
                            )
                            outcome.replayed = True
                            continue

                        doc = docs_by_id.get(plan.memory_id)
                        if plan.action == "create":
                            if doc is not None and doc["active_version"] is not None:
                                raise MemoryVersionConflictError(
                                    f"{plan.memory_id} 已存在活动版本",
                                    field="expected_version",
                                )
                        else:
                            current_version = (
                                int(doc["active_version"])
                                if doc and doc["active_version"] is not None
                                else None
                            )
                            if current_version is None:
                                raise MemoryNotFoundError(plan.memory_id)
                            if plan.expected_version != current_version:
                                raise MemoryVersionConflictError(
                                    f"{plan.memory_id} 版本冲突: 期望 {plan.expected_version}, "
                                    f"当前 {current_version}",
                                    field="expected_version",
                                )

                        topic_title = (
                            plan.topic_title
                            or (doc["topic_title"] if doc else None)
                            or (topic_key or "学习者档案")
                        )
                        await docs_repo.upsert_document(
                            session,
                            user_id=user_id,
                            memory_id=plan.memory_id,
                            memory_type=plan.target_memory_type,
                            topic_key=topic_key,
                            topic_title=topic_title,
                            logical_path=logical_path_for(plan.memory_id),
                        )
                        await docs_repo.set_active_version(
                            session,
                            user_id=user_id,
                            memory_id=plan.memory_id,
                            active_version=after_version,
                            active_storage_key=stored.storage_key,
                            active_checksum=stored.checksum,
                        )
                        evidence_refs = evidence_refs_by_plan[i] if evidence_refs_by_plan else []
                        await commits_repo.insert_commit(
                            session,
                            commit_id=uuid4(),
                            mutation_id=plan.mutation_id,
                            operation_id=operation_id,
                            user_id=user_id,
                            memory_id=plan.memory_id,
                            action=plan.action,
                            before_version=before_version,
                            after_version=after_version,
                            storage_key=stored.storage_key,
                            checksum=stored.checksum,
                            actor_type=actor_type,
                            evidence_refs=evidence_refs[:100],
                            commit_payload={
                                "reason": plan.reason,
                                "candidate_indexes": plan.candidate_indexes,
                            },
                            prompt_version=prompt_version,
                            model_name=model_name,
                        )
                        await self._upsert_index_entry(
                            session,
                            user_id=user_id,
                            memory_id=plan.memory_id,
                            memory_type=plan.target_memory_type,
                            topic_key=topic_key,
                            source_version=after_version,
                            index_data=index_data,
                            evidence_refs=evidence_refs,
                            now=now,
                        )
                        await docs_repo.mark_index_dirty(session, user_id=user_id, dirty_at=now)
                        # Outbox 事件（§15 触发规则）
                        node_ids = graph_node_ids_by_plan[i] if graph_node_ids_by_plan else []
                        if plan.target_memory_type == "mastery":
                            await outbox_repo.insert_event(
                                session,
                                outbox_id=uuid4(),
                                operation_id=operation_id,
                                user_id=user_id,
                                event_type="memory.changed",
                                aggregate_type="memory",
                                aggregate_id=plan.memory_id,
                                aggregate_version=after_version,
                                payload={
                                    "schema_version": 1,
                                    "memory_id": plan.memory_id,
                                    "memory_type": "mastery",
                                    "before_version": before_version,
                                    "after_version": after_version,
                                    "topic_key": topic_key,
                                    "graph_projection_candidates": node_ids[:20],
                                },
                            )
                            # mastery 活动版本提交后维护 link（§13.8.1）。评审 I-3：
                            # **绝不"先全灭再重建"**——不是每次提交都携带图谱信息
                            # （frontmatter_patch / keywords 治理 / 悬空候选建档的
                            # graph_node_ids 为空），全灭会让该主题的 KG 映射整片
                            # inactive，紧随其后的 `_dual_write_kg` 因
                            # `active=true AND memory_version=:version` 不成立而必然
                            # skipped/no_graph_mapping。
                            await self._sync_graph_links(
                                session,
                                user_id=user_id,
                                memory_id=plan.memory_id,
                                active_version=after_version,
                                node_ids=node_ids,
                                mapping_method=(
                                    mapping_methods_by_plan[i] if mapping_methods_by_plan else None
                                ),
                                mapping_confidence=(
                                    mapping_confidences_by_plan[i]
                                    if mapping_confidences_by_plan
                                    else None
                                ),
                            )
                        else:
                            changed_sections = index_data.get("changed_sections") or [
                                "preferences",
                                "goals",
                                "plans",
                            ]
                            await outbox_repo.insert_event(
                                session,
                                outbox_id=uuid4(),
                                operation_id=operation_id,
                                user_id=user_id,
                                event_type="learner.updated",
                                aggregate_type="memory",
                                aggregate_id="learner",
                                aggregate_version=after_version,
                                payload={
                                    "schema_version": 1,
                                    "memory_id": "learner",
                                    "before_version": before_version,
                                    "after_version": after_version,
                                    "changed_sections": changed_sections[:3],
                                },
                            )
                        results_by_index[i] = MutationResult(
                            mutation_id=plan.mutation_id,
                            memory_id=plan.memory_id,
                            action=plan.action,
                            before_version=before_version,
                            after_version=after_version,
                        )

        finally:
            await self._clear_commit_started(
                operation_id,
                expected_worker=expected_worker,
                expected_generation=expected_generation,
            )

        # 按原 plans 顺序汇总（replay 命中与新提交混排时保持返回顺序稳定）
        outcome.mutations = [results_by_index[i] for i in range(len(plans))]

        # 3. 物化 current/（失败不影响活动版本，§8.6）；replay 命中的 plan
        #    不在 rendered 中，不会重写 current/（评审 #5）
        for _i, plan, content, _bv, _av, _tk, _idx in rendered:
            try:
                await self._store.materialize_current(
                    user_id=user_id, memory_id=plan.memory_id, content=content
                )
            except OSError as exc:
                outcome.warnings.append(
                    f"current 物化失败 {plan.memory_id}: {type(exc).__name__}，维护任务将修复"
                )
        return outcome

    async def _sync_graph_links(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        memory_id: str,
        active_version: int,
        node_ids: list[str],
        mapping_method: str | None,
        mapping_confidence: float | None,
    ) -> None:
        """把 ``memory_graph_links`` 推进到新活动版本（评审 I-3，§13.8.1）。

        三种情况，判据都是"这次提交到底知不知道图谱映射"：

        1. 携带节点（``node_ids`` 非空）：这些节点按新版本 upsert（active=true），
           不再出现的旧 link 置 inactive——这才是"映射被改写"；
        2. **不携带节点但该记忆已有活动 link**（frontmatter_patch / keywords 治理 /
           悬空候选建档）：按既有 link 的 node_id 在新版本上重新 upsert，保持 active，
           **不删除任何映射**。映射没变、只是版本推进了，这正是"先全灭再重建"错杀的场景；
        3. 两者都没有（该记忆从来没有 KG 映射）：不触碰 ``memory_graph_links``。

        真正"要删除映射"的场景不受影响：``forget`` / purge 各自显式 deactivate 全部
        link；``restore`` 由调用方重新绑定；调用方给出节点集合时，消失的节点依旧被置
        inactive（情况 1）。
        """
        from backend.memory.persistence import graph_states as gs_repo

        previous_links = await gs_repo.list_active_links_for_memory(
            session, user_id=user_id, memory_id=memory_id, active_version=active_version - 1
        )
        if node_ids:
            previous_by_node = {str(link["node_id"]): link for link in previous_links}
            for node_id in node_ids:
                previous = previous_by_node.get(node_id)
                # mapping_method 有 CHECK 白名单（explicit_hint/exact_alias/model_candidate），
                # confidence 有 BETWEEN 0 AND 1：两列都 NOT NULL，既不能留空也不能编造。
                # 本轮没给就沿用旧行的值；旧行也没有（新映射没带元数据）时只能跳过——
                # 宁可不建这条 link，也不能写一个违反约束的"未知来源"。
                method = mapping_method or str((previous or {}).get("mapping_method") or "")
                if not method:
                    continue
                confidence = (
                    mapping_confidence
                    if mapping_confidence is not None
                    else float((previous or {}).get("mapping_confidence") or 0.0)
                )
                await gs_repo.upsert_graph_link(
                    session,
                    user_id=user_id,
                    memory_id=memory_id,
                    node_id=node_id,
                    memory_version=active_version,
                    mapping_method=method,
                    mapping_confidence=min(max(confidence, 0.0), 1.0),
                )
            # 节点集合以本次提交为准：不再出现的旧 link 置 inactive（§16.4）
            for kept_node_id in node_ids:
                await gs_repo.deactivate_graph_links(
                    session, user_id=user_id, memory_id=memory_id, except_node_id=kept_node_id
                )
            return
        # 情况 2：本次提交没有图谱信息 —— 既有映射一个都不删，只把版本推上去。
        for link in previous_links:
            await gs_repo.upsert_graph_link(
                session,
                user_id=user_id,
                memory_id=memory_id,
                node_id=str(link["node_id"]),
                memory_version=active_version,
                mapping_method=str(link["mapping_method"]),
                mapping_confidence=float(link["mapping_confidence"]),
            )

    async def _upsert_index_entry(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        memory_id: str,
        memory_type: str,
        topic_key: str | None,
        source_version: int,
        index_data: dict[str, Any],
        evidence_refs: list[str],
        now: datetime,
    ) -> None:
        await session.execute(
            text(
                """
                INSERT INTO memory_index_entries (
                    user_id, memory_id, source_version, memory_type, topic_key,
                    title, summary, keywords, aliases, related_topic_keys,
                    search_text, evidence_refs, updated_at
                ) VALUES (
                    :user_id, :memory_id, :source_version, :memory_type, :topic_key,
                    :title, :summary, :keywords, :aliases, :related_topic_keys,
                    :search_text, CAST(:evidence_refs AS jsonb), :updated_at
                )
                ON CONFLICT (user_id, memory_id) DO UPDATE
                SET source_version = EXCLUDED.source_version,
                    title = EXCLUDED.title, summary = EXCLUDED.summary,
                    keywords = EXCLUDED.keywords, aliases = EXCLUDED.aliases,
                    related_topic_keys = EXCLUDED.related_topic_keys,
                    search_text = EXCLUDED.search_text,
                    evidence_refs = EXCLUDED.evidence_refs,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "user_id": user_id,
                "memory_id": memory_id,
                "source_version": source_version,
                "memory_type": memory_type,
                "topic_key": topic_key,
                "title": index_data["title"],
                "summary": index_data["summary"][:2000],
                "keywords": index_data["keywords"],
                # v2 投影（§3.4）：旧调用方可能不传这两项，用 get 兼容
                "aliases": list(index_data.get("aliases") or []),
                "related_topic_keys": list(index_data.get("related_topic_keys") or []),
                "search_text": index_data["search_text"],
                "evidence_refs": json.dumps(evidence_refs[:100], ensure_ascii=False),
                "updated_at": now,
            },
        )

    # ---------------- 删除与恢复（§8.7） ----------------

    async def forget(
        self,
        *,
        operation_id: UUID,
        user_id: UUID,
        actor_type: str,
        mutation_id: UUID,
        memory_id: str,
        expected_version: int,
        reason: str | None,
    ) -> MutationResult:
        now = _now()
        tombstone_until = now + timedelta(days=self._settings.memory_tombstone_days)
        async with self._session_factory() as session:
            async with session.begin():
                await acquire_user_lock(session, user_id)
                # mutation replay 优先于状态检查（评审 #6）：同 mutation 重放
                # 直接返回原结果，只有不存在 replay 时才检查当前状态和版本
                existing = await commits_repo.get_by_mutation_id(session, mutation_id)
                if existing is not None:
                    _validate_replay_consistency(
                        existing, user_id=user_id, memory_id=memory_id, action="forget"
                    )
                    return MutationResult(
                        mutation_id=mutation_id,
                        memory_id=memory_id,
                        action="forget",
                        before_version=existing["before_version"],
                        after_version=None,
                    )
                docs = await docs_repo.lock_documents(
                    session, user_id=user_id, memory_ids=[memory_id]
                )
                doc = docs[0] if docs else None
                if doc is None or doc["active_version"] is None:
                    if doc is not None and doc["deleted_at"] is not None:
                        raise MemoryDeletedError(memory_id)
                    raise MemoryNotFoundError(memory_id)
                current_version = int(doc["active_version"])
                if expected_version != current_version:
                    raise MemoryVersionConflictError(
                        f"{memory_id} 版本冲突", field="expected_version"
                    )
                deleted_version = current_version
                await commits_repo.insert_commit(
                    session,
                    commit_id=uuid4(),
                    mutation_id=mutation_id,
                    operation_id=operation_id,
                    user_id=user_id,
                    memory_id=memory_id,
                    action="forget",
                    before_version=deleted_version,
                    after_version=None,
                    storage_key=None,
                    checksum=None,
                    actor_type=actor_type,
                    evidence_refs=[],
                    commit_payload={"reason": reason},
                    prompt_version=None,
                    model_name=None,
                )
                await docs_repo.tombstone_document(
                    session,
                    user_id=user_id,
                    memory_id=memory_id,
                    deleted_version=deleted_version,
                    deleted_at=now,
                    tombstone_until=tombstone_until,
                )
                await session.execute(
                    text(
                        "DELETE FROM memory_index_entries "
                        "WHERE user_id = :user_id AND memory_id = :memory_id"
                    ),
                    {"user_id": user_id, "memory_id": memory_id},
                )
                await docs_repo.mark_index_dirty(session, user_id=user_id, dirty_at=now)
                # 删除抑制：旧证据不得复活同一记忆（§8.7）
                old_refs = await self._load_evidence_refs_for_version(
                    session,
                    user_id=user_id,
                    memory_id=memory_id,
                    storage_key=doc["active_storage_key"],
                    memory_type=doc["memory_type"],
                )
                for ref in old_refs:
                    await session.execute(
                        text(
                            """
                            INSERT INTO memory_deleted_evidence_suppressions (
                                user_id, memory_id, evidence_ref_hash, hash_key_version
                            ) VALUES (:user_id, :memory_id, :hash, :version)
                            ON CONFLICT DO NOTHING
                            """
                        ),
                        {
                            "user_id": user_id,
                            "memory_id": memory_id,
                            "hash": evidence_ref_hash(self._settings.privacy_hmac_key, ref),
                            "version": self._settings.privacy_hmac_key_version,
                        },
                    )
                # 图谱 link 全部置 inactive（§16.4）；先取删除前 link 用于事件候选
                from backend.memory.persistence import graph_states as gs_repo

                links = await gs_repo.list_active_links_for_memory(
                    session,
                    user_id=user_id,
                    memory_id=memory_id,
                    active_version=deleted_version,
                )
                await gs_repo.deactivate_graph_links(session, user_id=user_id, memory_id=memory_id)
                # Outbox
                await outbox_repo.insert_event(
                    session,
                    outbox_id=uuid4(),
                    operation_id=operation_id,
                    user_id=user_id,
                    event_type="memory.deleted",
                    aggregate_type="memory",
                    aggregate_id=memory_id,
                    aggregate_version=deleted_version,
                    payload={
                        "schema_version": 1,
                        "memory_id": memory_id,
                        "memory_type": doc["memory_type"],
                        "deleted_version": deleted_version,
                        "restore_until": tombstone_until.isoformat(),
                        "graph_projection_candidates": [str(link["node_id"]) for link in links][
                            :20
                        ],
                    },
                )
        # 事务后：物化清理与隔离（失败可恢复，§8.7.5/6）
        await self._store.remove_current(user_id=user_id, memory_id=memory_id)
        try:
            await self._store.move_to_quarantine(
                user_id=user_id,
                memory_id=memory_id,
                deleted_version=deleted_version,
                deleted_at_epoch=int(now.timestamp()),
            )
        except OSError:
            pass
        return MutationResult(
            mutation_id=mutation_id,
            memory_id=memory_id,
            action="forget",
            before_version=deleted_version,
            after_version=None,
        )

    async def _load_evidence_refs_for_version(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        memory_id: str,
        storage_key: str,
        memory_type: str,
    ) -> list[str]:
        try:
            content = await self._store.read_version(user_id=user_id, storage_key=storage_key)
        except FileNotFoundError:
            return []
        if memory_type == "learner":
            return parse_learner(content.decode("utf-8")).evidence_refs
        return parse_mastery(content.decode("utf-8")).evidence_refs

    async def restore(
        self,
        *,
        operation_id: UUID,
        user_id: UUID,
        actor_type: str,
        mutation_id: UUID,
        memory_id: str,
        deleted_version: int,
        graph_node_ids: list[str] | None = None,
    ) -> MutationResult:
        now = _now()
        async with self._session_factory() as session:
            async with session.begin():
                await acquire_user_lock(session, user_id)
                # mutation replay 优先于状态检查（评审 #6）：restore 首次成功后
                # 重放同 mutation 不得因"已恢复"而抛版本冲突
                existing = await commits_repo.get_by_mutation_id(session, mutation_id)
                if existing is not None:
                    _validate_replay_consistency(
                        existing, user_id=user_id, memory_id=memory_id, action="restore"
                    )
                    return MutationResult(
                        mutation_id=mutation_id,
                        memory_id=memory_id,
                        action="restore",
                        before_version=None,
                        after_version=existing["after_version"],
                    )
                docs = await docs_repo.lock_documents(
                    session, user_id=user_id, memory_ids=[memory_id]
                )
                doc = docs[0] if docs else None
                if doc is None:
                    raise MemoryNotFoundError(memory_id)
                if doc["deleted_at"] is None:
                    raise MemoryVersionConflictError(
                        f"{memory_id} 未处于删除状态", field="deleted_version"
                    )
                if doc["deleted_version"] != deleted_version:
                    raise MemoryVersionConflictError(
                        f"{memory_id} deleted_version 不匹配", field="deleted_version"
                    )
                if now >= doc["tombstone_until"]:
                    raise MemoryRestoreExpiredError(f"{memory_id} 已超过 30 天恢复窗口")
                # 读取被删除版本正文并校验 checksum（§8.7.3）：
                # 先读不可变版本区，物化移动成功后回退读隔离区。
                old_commit = await self._find_commit_for_version(
                    session,
                    user_id=user_id,
                    memory_id=memory_id,
                    version=deleted_version,
                )
                if old_commit is None:
                    raise MemoryNotFoundError(f"{memory_id} 删除前版本无 commit 记录")
                try:
                    content = await self._store.read_version_by_id(
                        user_id=user_id,
                        memory_id=memory_id,
                        version=deleted_version,
                        checksum=old_commit["checksum"],
                    )
                except FileNotFoundError:
                    content = await self._store.read_quarantined_version(
                        user_id=user_id,
                        memory_id=memory_id,
                        version=deleted_version,
                        checksum=old_commit["checksum"],
                    )
                from backend.memory.storage.base import sha256_hex

                if sha256_hex(content) != old_commit["checksum"]:
                    from backend.memory.contracts.errors import StorageUnavailableError

                    raise StorageUnavailableError(f"{memory_id} 版本 checksum 校验失败")
                new_version = (
                    await docs_repo.get_max_version(session, user_id=user_id, memory_id=memory_id)
                    + 1
                )
                stored = await self._store.write_immutable_version(
                    user_id=user_id,
                    memory_id=memory_id,
                    version=new_version,
                    content=content,
                )
                await commits_repo.insert_commit(
                    session,
                    commit_id=uuid4(),
                    mutation_id=mutation_id,
                    operation_id=operation_id,
                    user_id=user_id,
                    memory_id=memory_id,
                    action="restore",
                    before_version=None,
                    after_version=new_version,
                    storage_key=stored.storage_key,
                    checksum=stored.checksum,
                    actor_type=actor_type,
                    evidence_refs=[],
                    commit_payload={"restored_from_version": deleted_version},
                    prompt_version=None,
                    model_name=None,
                )
                await docs_repo.restore_document(
                    session,
                    user_id=user_id,
                    memory_id=memory_id,
                    active_version=new_version,
                    active_storage_key=stored.storage_key,
                    active_checksum=stored.checksum,
                )
                # 重建检索索引：keywords/aliases 从恢复出的文档投影（§3.4 / 裁决 A）
                memory_type = doc["memory_type"]
                parsed: Any
                if memory_type == "learner":
                    parsed = parse_learner(content.decode("utf-8"))
                    index_data = {
                        "title": "学习者档案",
                        "summary": "；".join(
                            (parsed.goals or parsed.preferences or ["学习者档案"])[:3]
                        ),
                        "keywords": list(parsed.keywords),
                        "aliases": list(parsed.aliases),
                        "related_topic_keys": list(parsed.links),
                        "search_text": " ".join(
                            ["学习者档案", *parsed.preferences, *parsed.goals, *parsed.plans]
                        ),
                    }
                    evidence_refs = parsed.evidence_refs
                else:
                    parsed = parse_mastery(content.decode("utf-8"))
                    index_data = {
                        "title": parsed.topic_title,
                        "summary": parsed.overview or "；".join(parsed.understood[:3]),
                        "keywords": list(parsed.keywords),
                        "aliases": list(parsed.aliases),
                        "related_topic_keys": list(parsed.links),
                        "search_text": " ".join(
                            [
                                parsed.topic_title,
                                parsed.overview,
                                *parsed.understood,
                                *parsed.difficulties,
                                *parsed.review_advice,
                            ]
                        ),
                    }
                    evidence_refs = parsed.evidence_refs
                await self._upsert_index_entry(
                    session,
                    user_id=user_id,
                    memory_id=memory_id,
                    memory_type=memory_type,
                    topic_key=doc["topic_key"],
                    source_version=new_version,
                    index_data=index_data,
                    evidence_refs=evidence_refs,
                    now=now,
                )
                await docs_repo.mark_index_dirty(session, user_id=user_id, dirty_at=now)
                await outbox_repo.insert_event(
                    session,
                    outbox_id=uuid4(),
                    operation_id=operation_id,
                    user_id=user_id,
                    event_type="memory.restored",
                    aggregate_type="memory",
                    aggregate_id=memory_id,
                    aggregate_version=new_version,
                    payload={
                        "schema_version": 1,
                        "memory_id": memory_id,
                        "memory_type": memory_type,
                        "restored_from_version": deleted_version,
                        "after_version": new_version,
                        "graph_projection_candidates": (graph_node_ids or [])[:20],
                    },
                )
        await self._store.materialize_current(user_id=user_id, memory_id=memory_id, content=content)
        return MutationResult(
            mutation_id=mutation_id,
            memory_id=memory_id,
            action="restore",
            before_version=None,
            after_version=new_version,
        )

    async def _find_commit_for_version(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        memory_id: str,
        version: int,
    ) -> dict[str, Any] | None:
        result = await session.execute(
            text(
                "SELECT * FROM memory_commits "
                "WHERE user_id = :user_id AND memory_id = :memory_id "
                "AND after_version = :version AND checksum IS NOT NULL "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"user_id": user_id, "memory_id": memory_id, "version": version},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    # ---------------- index.md 确定性重建（§8.6.1） ----------------

    async def rebuild_index(self, *, user_id: UUID, operation_id: UUID) -> dict[str, Any]:
        """只索引当前未删除活动版本；并发 commit 时不得清除新 dirty 标记。

        §3.4：index 是**可再生的派生物**，条目字段（name/description/aliases/
        keywords/related）全部由文档投影而来。这里直接读提交时写进 PG 的同一份投影
        （``memory_index_entries``），投影行缺失时相应字段退化为空——重建宁可少写
        一个字段，也不能失败或编造内容。
        """
        now = _now()
        async with self._session_factory() as session:
            async with session.begin():
                await acquire_user_lock(session, user_id)
                index_doc = await docs_repo.get_document(
                    session, user_id=user_id, memory_id="index"
                )
                if index_doc is None or index_doc["index_dirty_at"] is None:
                    return {"rebuilt": False, "reason": "not_dirty"}
                expected_dirty_at = index_doc["index_dirty_at"]
                actives = await docs_repo.list_active_documents(session, user_id=user_id)
                entries: list[IndexEntry] = []
                learner_entry: IndexEntry | None = None
                for doc in actives:
                    if doc["memory_type"] == "index":
                        continue
                    projection = await self._index_projection(
                        session, user_id=user_id, memory_id=doc["memory_id"]
                    )
                    entry = self._index_entry_from_projection(doc, projection)
                    if doc["memory_type"] == "learner":
                        learner_entry = entry
                    else:
                        entries.append(entry)
                new_version = int(index_doc["active_version"] or 0) + 1
                index = IndexDocument(
                    user_id=user_id,
                    version=new_version,
                    updated_at=now,
                    # §2.7 决议 A 组「接受 index 格式升版」：重建即产出 v2 块结构，
                    # 否则 render_index 走 v1 单行分支，description/aliases/keywords
                    # 全部投影不出来（迁移任务明确不处理 index，由本函数负责升版）。
                    schema_version=SCHEMA_VERSION_V2,
                    learner=learner_entry,
                    mastery_entries=sorted(entries, key=lambda e: e.memory_id),
                    # §5.9④：悬空 `[[link]]` 的候选主题区——事实源是 PG 的
                    # memory_dangling_links（跨批次计数），index 只是它的投影。
                    candidate_topics=await self._candidate_topic_labels(session, user_id=user_id),
                )
                content = render_index(index).encode("utf-8")
                stored = await self._store.write_immutable_version(
                    user_id=user_id, memory_id="index", version=new_version, content=content
                )
                await docs_repo.set_active_version(
                    session,
                    user_id=user_id,
                    memory_id="index",
                    active_version=new_version,
                    active_storage_key=stored.storage_key,
                    active_checksum=stored.checksum,
                )
                cleared = await docs_repo.clear_index_dirty(
                    session, user_id=user_id, expected_dirty_at=expected_dirty_at
                )
                # 审计：rebuild_index commit，不产生业务事件（§8.6.1）
                await commits_repo.insert_commit(
                    session,
                    commit_id=uuid4(),
                    mutation_id=uuid4(),
                    operation_id=operation_id,
                    user_id=user_id,
                    memory_id="index",
                    action="rebuild_index",
                    before_version=int(index_doc["active_version"] or 0) or None,
                    after_version=new_version,
                    storage_key=stored.storage_key,
                    checksum=stored.checksum,
                    actor_type="system",
                    evidence_refs=[],
                    commit_payload={
                        "expected_dirty_at": expected_dirty_at.isoformat(),
                        "dirty_cleared": cleared,
                        "source_versions": {
                            d["memory_id"]: int(d["active_version"])
                            for d in actives
                            if d["memory_type"] != "index"
                        },
                    },
                    prompt_version=None,
                    model_name=None,
                )
        try:
            await self._store.materialize_current(
                user_id=user_id, memory_id="index", content=content
            )
        except OSError:
            pass
        return {"rebuilt": True, "version": new_version, "dirty_cleared": cleared}

    async def refresh_index_projection(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        memory_id: str,
        now: datetime | None = None,
    ) -> bool:
        """按当前活动版本重建单个文档的检索投影（§3.4；评审 I-11① 的迁移接线入口）。

        存在的理由：``migrate_markdown_schema_v2`` 直接改文档活动版本（v1→v2），
        而投影只写在提交路径上——迁移完之后 ``aliases / keywords / related`` 会一直
        为空，直到下一次记忆提交。0008 迁移的 docstring 把"回填"记在这个任务名下，
        所以维护路径需要一个**不改文档内容、只刷新投影**的入口。

        语义与提交路径同源：读活动版本正文 → 解析 → 用与 ``_build_new_content``
        相同的规则组装投影数据 → upsert ``memory_index_entries`` → 标 index dirty。
        调用方负责事务与提交（迁移处理器与它的 ``set_active_version`` 在同一事务里）。
        返回是否真的刷新了投影（文档不可读/已删除时 False，由调用方决定怎么记账）。
        """
        loaded = await self._load_active_document(session, user_id=user_id, memory_id=memory_id)
        if loaded is None:
            return False
        row, doc = loaded
        if isinstance(doc, LearnerDocument):
            index_data: dict[str, Any] = {
                "title": "学习者档案",
                "summary": "；".join((doc.goals or doc.preferences or ["学习者档案"])[:3]),
                "keywords": list(doc.keywords),
                "aliases": list(doc.aliases),
                "related_topic_keys": list(doc.links),
                "search_text": " ".join(
                    ["学习者档案", *doc.aliases, *doc.preferences, *doc.goals, *doc.plans]
                ),
            }
            evidence_refs = list(doc.evidence_refs)
            memory_type = "learner"
            topic_key = None
        elif isinstance(doc, MasteryDocument):
            projected_title = (doc.name or doc.topic_title) if _is_v2(doc) else doc.topic_title
            index_data = {
                "title": projected_title,
                "summary": doc.overview or "；".join(doc.understood[:3]),
                "keywords": list(doc.keywords),
                "aliases": list(doc.aliases),
                "related_topic_keys": list(doc.links),
                "search_text": " ".join(
                    [
                        doc.topic_title,
                        *doc.aliases,
                        doc.overview,
                        *doc.understood,
                        *doc.difficulties,
                        *doc.review_advice,
                    ]
                ),
            }
            evidence_refs = list(doc.evidence_refs)
            memory_type = "mastery"
            topic_key = doc.topic_key
        else:  # pragma: no cover - _load_active_document 只返回这两种类型
            return False
        active_version = int(row["active_version"])
        await self._upsert_index_entry(
            session,
            user_id=user_id,
            memory_id=memory_id,
            memory_type=memory_type,
            topic_key=topic_key,
            source_version=active_version,
            index_data=index_data,
            evidence_refs=evidence_refs,
            now=now or _now(),
        )
        await docs_repo.mark_index_dirty(session, user_id=user_id, dirty_at=now or _now())
        return True

    async def _candidate_topic_labels(self, session: AsyncSession, *, user_id: UUID) -> list[str]:
        """§5.9④ 候选主题区的投影：还没到建档门槛的悬空链接（带批次数）。"""
        from backend.memory.persistence import dangling_links as dangling_repo

        rows = await dangling_repo.list_user_links(session, user_id=user_id, status="candidate")
        return [f"{row['target']}（{int(row['sighting_batches'])} 批）" for row in rows]

    async def _index_projection(
        self, session: AsyncSession, *, user_id: UUID, memory_id: str
    ) -> dict[str, Any] | None:
        """读 ``memory_index_entries`` 的 v2 投影列（§3.4）。

        提交路径在同一事务里把文档渲染结果写进这张表，因此它就是 index.md 需要的
        那份投影；重建时直接复用，不再解析一遍 Markdown，避免两处规则各自演化。
        """
        result = await session.execute(
            text(
                "SELECT title, summary, aliases, keywords, related_topic_keys, updated_at "
                "FROM memory_index_entries "
                "WHERE user_id = :user_id AND memory_id = :memory_id"
            ),
            {"user_id": user_id, "memory_id": memory_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    @staticmethod
    def _index_entry_from_projection(
        doc: dict[str, Any], projection: dict[str, Any] | None
    ) -> IndexEntry:
        """由 memory_documents 行 + 索引投影行构造 index.md 条目（§3.4）。

        投影行缺失（例如历史数据未回填）时 v2 字段退化为空、updated_at 退回文档行：
        重建必须照常产出 index.md，缺字段只是投影不完整，不能变成重建失败。
        """
        raw_updated = (projection or {}).get("updated_at") or doc["updated_at"]
        updated_at = (
            raw_updated
            if isinstance(raw_updated, datetime)
            else datetime.fromisoformat(str(raw_updated))
        )

        def _strings(key: str) -> list[str]:
            return [str(item) for item in ((projection or {}).get(key) or [])]

        return IndexEntry(
            memory_id=doc["memory_id"],
            memory_type=doc["memory_type"],
            topic_key=doc["topic_key"],
            title=doc["topic_title"] or "学习者档案",
            version=int(doc["active_version"]),
            updated_at=updated_at,
            description=str((projection or {}).get("summary") or ""),
            aliases=_strings("aliases"),
            related_topic_keys=_strings("related_topic_keys"),
            keywords=_strings("keywords"),
        )
