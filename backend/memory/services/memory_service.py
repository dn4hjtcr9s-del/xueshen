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
    LearnerPatch,
    LearnerReplacement,
    MasteryPatch,
    MasteryReplacement,
)
from backend.memory.contracts.common import evidence_ref_hash
from backend.memory.contracts.errors import (
    MemoryDeletedError,
    MemoryNotFoundError,
    MemoryRestoreExpiredError,
    MemoryVersionConflictError,
)
from backend.memory.contracts.operations import MutationResult
from backend.memory.persistence import commits as commits_repo
from backend.memory.persistence import documents as docs_repo
from backend.memory.persistence import outbox as outbox_repo
from backend.memory.persistence.database import acquire_user_lock
from backend.memory.storage.base import MarkdownStore, logical_path_for
from backend.memory.storage.markdown_schema import (
    IndexDocument,
    IndexEntry,
    LearnerDocument,
    MasteryDocument,
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


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


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
                "keywords": [],
                "search_text": " ".join(
                    ["学习者档案", *base.preferences, *base.goals, *base.plans]
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
        if plan.replacement is not None:
            assert isinstance(plan.replacement, MasteryReplacement)
            mastery_from_replacement(mbase, plan.replacement)
        after_version = (before_version or 0) + 1
        mbase.version = after_version
        mbase.updated_at = now
        content = render_mastery(mbase).encode("utf-8")
        index_data = {
            "title": mbase.topic_title,
            "summary": mbase.overview or "；".join(mbase.understood[:3]),
            "keywords": [],
            "search_text": " ".join(
                [
                    mbase.topic_title,
                    mbase.overview,
                    *mbase.understood,
                    *mbase.difficulties,
                    *mbase.review_advice,
                ]
            ),
        }
        return content, before_version, after_version, topic_key, index_data

    # ---------------- 原子提交 ----------------

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
    ) -> CommitOutcome:
        """多文档原子提交（§8.6）。任何校验失败整个事务回滚。"""
        if len(plans) > MAX_PLANS_PER_OPERATION:
            raise ValueError(f"一个 operation 最多 {MAX_PLANS_PER_OPERATION} 个 CommitMutationPlan")
        now = _now()
        outcome = CommitOutcome()

        # 1. 事务外渲染内容并写不可变版本
        rendered: list[
            tuple[CommitMutationPlan, bytes, int | None, int, str | None, dict[str, Any]]
        ] = []
        async with self._session_factory() as session:
            for plan in plans:
                rendered.append(
                    (
                        plan,
                        *await self._build_new_content(
                            session, user_id=user_id, plan=plan, now=now
                        ),
                    )
                )
        stored_by_plan: list[Any] = []
        for plan, content, _bv, av, _tk, _idx in rendered:
            stored = await self._store.write_immutable_version(
                user_id=user_id, memory_id=plan.memory_id, version=av, content=content
            )
            stored_by_plan.append(stored)

        # 2. 数据库事务
        async with self._session_factory() as session:
            async with session.begin():
                await acquire_user_lock(session, user_id)
                memory_ids = sorted({p.memory_id for p in plans})
                locked_docs = await docs_repo.lock_documents(
                    session, user_id=user_id, memory_ids=memory_ids
                )
                docs_by_id = {d["memory_id"]: d for d in locked_docs}

                for i, (
                    plan,
                    _content,
                    before_version,
                    after_version,
                    topic_key,
                    index_data,
                ) in enumerate(rendered):
                    stored = stored_by_plan[i]
                    # mutation_id 重放：直接返回原 commit（§11.3）
                    existing = await commits_repo.get_by_mutation_id(session, plan.mutation_id)
                    if existing is not None:
                        outcome.mutations.append(
                            MutationResult(
                                mutation_id=plan.mutation_id,
                                memory_id=plan.memory_id,
                                action=existing["action"],
                                before_version=existing["before_version"],
                                after_version=existing["after_version"],
                            )
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
                        # mastery 活动版本提交后 upsert link（§13.8.1）：
                        # 先把旧 link 全部置 inactive，再按当前映射重建
                        from backend.memory.persistence import graph_states as gs_repo

                        method = mapping_methods_by_plan[i] if mapping_methods_by_plan else None
                        confidence = (
                            mapping_confidences_by_plan[i] if mapping_confidences_by_plan else None
                        )
                        await gs_repo.deactivate_graph_links(
                            session, user_id=user_id, memory_id=plan.memory_id
                        )
                        for node_id in node_ids:
                            if method and confidence is not None:
                                await gs_repo.upsert_graph_link(
                                    session,
                                    user_id=user_id,
                                    memory_id=plan.memory_id,
                                    node_id=node_id,
                                    memory_version=after_version,
                                    mapping_method=method,
                                    mapping_confidence=confidence,
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
                    outcome.mutations.append(
                        MutationResult(
                            mutation_id=plan.mutation_id,
                            memory_id=plan.memory_id,
                            action=plan.action,
                            before_version=before_version,
                            after_version=after_version,
                        )
                    )

        # 3. 物化 current/（失败不影响活动版本，§8.6）
        for plan, content, _bv, _av, _tk, _idx in rendered:
            try:
                await self._store.materialize_current(
                    user_id=user_id, memory_id=plan.memory_id, content=content
                )
            except OSError as exc:
                outcome.warnings.append(
                    f"current 物化失败 {plan.memory_id}: {type(exc).__name__}，维护任务将修复"
                )
        return outcome

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
                    title, summary, keywords, search_text, evidence_refs, updated_at
                ) VALUES (
                    :user_id, :memory_id, :source_version, :memory_type, :topic_key,
                    :title, :summary, :keywords, :search_text,
                    CAST(:evidence_refs AS jsonb), :updated_at
                )
                ON CONFLICT (user_id, memory_id) DO UPDATE
                SET source_version = EXCLUDED.source_version,
                    title = EXCLUDED.title, summary = EXCLUDED.summary,
                    keywords = EXCLUDED.keywords, search_text = EXCLUDED.search_text,
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
                existing = await commits_repo.get_by_mutation_id(session, mutation_id)
                if existing is not None:
                    return MutationResult(
                        mutation_id=mutation_id,
                        memory_id=memory_id,
                        action="forget",
                        before_version=existing["before_version"],
                        after_version=None,
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
                existing = await commits_repo.get_by_mutation_id(session, mutation_id)
                if existing is not None:
                    return MutationResult(
                        mutation_id=mutation_id,
                        memory_id=memory_id,
                        action="restore",
                        before_version=None,
                        after_version=existing["after_version"],
                    )
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
                # 重建检索索引
                memory_type = doc["memory_type"]
                parsed: Any
                if memory_type == "learner":
                    parsed = parse_learner(content.decode("utf-8"))
                    index_data = {
                        "title": "学习者档案",
                        "summary": "；".join(
                            (parsed.goals or parsed.preferences or ["学习者档案"])[:3]
                        ),
                        "keywords": [],
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
                        "keywords": [],
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
        """只索引当前未删除活动版本；并发 commit 时不得清除新 dirty 标记。"""
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
                    updated = await self._doc_updated_at(session, doc)
                    entry = IndexEntry(
                        memory_id=doc["memory_id"],
                        memory_type=doc["memory_type"],
                        topic_key=doc["topic_key"],
                        title=doc["topic_title"] or "学习者档案",
                        version=int(doc["active_version"]),
                        updated_at=updated,
                    )
                    if doc["memory_type"] == "learner":
                        learner_entry = entry
                    else:
                        entries.append(entry)
                new_version = int(index_doc["active_version"] or 0) + 1
                index = IndexDocument(
                    user_id=user_id,
                    version=new_version,
                    updated_at=now,
                    learner=learner_entry,
                    mastery_entries=sorted(entries, key=lambda e: e.memory_id),
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

    async def _doc_updated_at(self, session: AsyncSession, doc: dict[str, Any]) -> datetime:
        result = await session.execute(
            text(
                "SELECT updated_at FROM memory_index_entries "
                "WHERE user_id = :user_id AND memory_id = :memory_id"
            ),
            {"user_id": doc["user_id"], "memory_id": doc["memory_id"]},
        )
        value = result.scalar()
        if value is not None:
            return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        result2 = doc["updated_at"]
        return result2 if isinstance(result2, datetime) else datetime.fromisoformat(str(result2))
