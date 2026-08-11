"""KnowledgeGraphStateService：用户图谱命令与总结投影的确定性状态机。

对应规格 §2.5 / §10.5 / §10.6 / §16.2 / §16.3 / §16.5。
对外只允许四种状态：无状态(null) / learning / proficient / expert。
expert 只能由长期、多次、高质量证据推导，用户不可手动设置。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.commands import ProjectSummaryToGraphCommand
from backend.memory.contracts.errors import (
    GraphNodeNotFoundError,
    GraphStateVersionConflictError,
    GraphStateVersionRequiredError,
)
from backend.memory.contracts.evidence import GraphProjectionEvidence
from backend.memory.contracts.graph_state import GraphRecommendation
from backend.memory.contracts.operations import GraphStateChangeView
from backend.memory.knowledge_graph.registry import KnowledgeGraphRegistry
from backend.memory.persistence import documents as docs_repo
from backend.memory.persistence import graph_states as gs_repo
from backend.memory.persistence import outbox as outbox_repo
from backend.memory.persistence.database import acquire_user_lock
from backend.settings import Settings

GraphStatus = Literal["learning", "proficient", "expert"]

# 用户动作转换表（§16.2）：任何当前状态 + 动作 → 新状态（None = 无状态）
USER_TRANSITIONS: dict[tuple[str | None, str], str | None] = {
    (None, "mark_unfamiliar"): "learning",
    (None, "mark_familiar"): "proficient",
    (None, "clear"): None,
    ("learning", "mark_unfamiliar"): "learning",
    ("learning", "mark_familiar"): "proficient",
    ("learning", "clear"): None,
    ("proficient", "mark_unfamiliar"): "learning",
    ("proficient", "mark_familiar"): "proficient",
    ("proficient", "clear"): None,
    ("expert", "mark_unfamiliar"): "learning",
    ("expert", "mark_familiar"): "proficient",
    ("expert", "clear"): None,
}

# 自主解答/推导/迁移/讲解类证据 ref 关键词（expert 质量条件，§9.3）
_SELF_DEMONSTRATION_MARKERS = ("user_solution", "exercise_completed", "explicit_remember")


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass
class EvidenceEvaluation:
    status: GraphStatus | None
    reason_codes: list[str] = field(default_factory=list)
    qualifying_refs: list[str] = field(default_factory=list)


def evaluate_evidence(
    evidence: list[GraphProjectionEvidence], *, settings: Settings, now: datetime
) -> EvidenceEvaluation:
    """证据聚合确定性规则（§16.3 / §9.3）。

    - 只统计窗口内、去重后的证据；正向需 strength >= GRAPH_POSITIVE_STRENGTH。
    - learning：一条有实质内容的学习证据（direction=learning）。
    - proficient：>=2 条独立正向证据。
    - expert：>=3 条 strong_positive，最早最晚跨度 >= GRAPH_EXPERT_MIN_SPAN_DAYS，
      且至少一条自主解答/推导类证据，不存在未解决强冲突。
    - 降级：>=2 条独立强冲突（裁决 2A：单条强冲突只阻止晋升、不降级）。
    """
    window_start = now - timedelta(days=settings.graph_evidence_window_days)
    seen: set[str] = set()
    valid: list[GraphProjectionEvidence] = []
    for item in evidence:
        if item.occurred_at.tzinfo is None:
            item = item.model_copy(update={"occurred_at": item.occurred_at.replace(tzinfo=UTC)})
        if item.occurred_at < window_start:
            continue
        if item.evidence_ref in seen:
            continue
        seen.add(item.evidence_ref)
        valid.append(item)

    strong_conflicts = [
        e
        for e in valid
        if e.direction == "conflict" and e.strength >= settings.graph_strong_conflict_strength
    ]
    positives = [
        e
        for e in valid
        if e.direction in ("positive", "strong_positive")
        and e.strength >= settings.graph_positive_strength
    ]
    strong_positives = [e for e in positives if e.direction == "strong_positive"]
    learnings = [e for e in valid if e.direction == "learning"]

    # 降级优先：>=2 独立强冲突（§9.3）。
    # 裁决 2A（2026-08-11）：第一版证据契约无"核心误解"标记，单条强冲突只阻止晋升、不降级。
    if len(strong_conflicts) >= 2:
        return EvidenceEvaluation(
            status="learning",
            reason_codes=["REVIEW_AFTER_CONFLICT"],
            qualifying_refs=[e.evidence_ref for e in strong_conflicts][:10],
        )

    # expert：未解决强冲突存在时不可晋升
    if len(strong_positives) >= 3 and not strong_conflicts:
        has_self_demo = any(
            marker in e.evidence_ref
            for e in strong_positives
            for marker in _SELF_DEMONSTRATION_MARKERS
        )
        if has_self_demo:
            span = max(e.occurred_at for e in strong_positives) - min(
                e.occurred_at for e in strong_positives
            )
            if span >= timedelta(days=settings.graph_expert_min_span_days):
                return EvidenceEvaluation(
                    status="expert",
                    reason_codes=["SUMMARY_MEMORY_SIGNAL"],
                    qualifying_refs=[e.evidence_ref for e in strong_positives][:10],
                )
        # 跨度或质量不足：最多维持 proficient

    if len(positives) >= 2:
        return EvidenceEvaluation(
            status="proficient",
            reason_codes=["SUMMARY_MEMORY_SIGNAL"],
            qualifying_refs=[e.evidence_ref for e in positives][:10],
        )

    if learnings or positives:
        refs = [e.evidence_ref for e in (learnings + positives)][:10]
        return EvidenceEvaluation(
            status="learning", reason_codes=["CONTINUE_LEARNING"], qualifying_refs=refs
        )

    return EvidenceEvaluation(status=None, reason_codes=[], qualifying_refs=[])


@dataclass
class ProjectionOutcome:
    changed: bool
    change: GraphStateChangeView | None = None
    warning: str | None = None


class KnowledgeGraphStateService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory

    # ---------------- 用户命令（§16.2） ----------------

    async def apply_user_command(
        self,
        *,
        operation_id: UUID,
        user_id: UUID,
        actor_type: str,
        node_id: str,
        action: Literal["mark_unfamiliar", "mark_familiar", "clear"],
        expected_version: int | None,
    ) -> GraphStateChangeView:
        now = _now()
        async with self._session_factory() as session:
            async with session.begin():
                registry = KnowledgeGraphRegistry(session)
                if not await registry.node_exists(node_id):
                    raise GraphNodeNotFoundError(node_id)
                await acquire_user_lock(session, user_id)
                overlays = await gs_repo.lock_overlays(session, user_id=user_id, node_ids=[node_id])
                overlay = overlays[0] if overlays else None
                before_status: str | None = overlay["status"] if overlay else None
                before_version: int | None = int(overlay["version"]) if overlay else None

                if overlay is not None:
                    if expected_version is None:
                        raise GraphStateVersionRequiredError(
                            "当前存在状态时必须携带 expected_version"
                        )
                    if expected_version != before_version:
                        raise GraphStateVersionConflictError(
                            f"节点 {node_id} 版本冲突", field="expected_version"
                        )
                elif expected_version is not None and action != "clear":
                    # 无状态首次标记可省略版本；携带了也不冲突
                    pass

                after_status = USER_TRANSITIONS[(before_status, action)]
                change = GraphStateChangeView(
                    node_id=node_id,
                    before_status=before_status,  # type: ignore[arg-type]
                    after_status=after_status,  # type: ignore[arg-type]
                    before_version=before_version,
                    after_version=None,
                    source_type="user",
                    reason_codes=[],
                    changed_at=now,
                )
                if after_status == before_status and action != "clear":
                    # 幂等 no_change：无版本推进，仍返回当前状态视图
                    change.after_version = before_version
                    return change

                if after_status is None:
                    if overlay is None:
                        change.after_version = None
                        return change  # 无状态 clear：幂等 no_change
                    deleted_version = await gs_repo.delete_overlay(
                        session, user_id=user_id, node_id=node_id
                    )
                    change.after_version = None
                    after_version_for_audit = None
                    before_version_for_audit = deleted_version
                else:
                    new_version = await gs_repo.upsert_overlay(
                        session,
                        user_id=user_id,
                        node_id=node_id,
                        status=after_status,
                        status_source="user",
                        source_memory_id=None,
                        source_memory_version=None,
                        evidence_snapshot=[],
                        evidence_count=0,
                        last_user_action_at=now,
                        last_evidence_at=None,
                    )
                    change.after_version = new_version
                    after_version_for_audit = new_version
                    before_version_for_audit = before_version

                audit_id = uuid4()
                await gs_repo.insert_audit(
                    session,
                    audit_id=audit_id,
                    operation_id=operation_id,
                    user_id=user_id,
                    node_id=node_id,
                    before_status=before_status,
                    after_status=after_status,
                    before_version=before_version_for_audit,
                    after_version=after_version_for_audit,
                    actor_type=actor_type,
                    reason_codes=[],
                    evidence_refs=[],
                    explanation_summary=None,
                )
                await outbox_repo.insert_event(
                    session,
                    outbox_id=uuid4(),
                    operation_id=operation_id,
                    user_id=user_id,
                    event_type="graph_state.changed",
                    aggregate_type="graph_node",
                    aggregate_id=node_id,
                    aggregate_version=change.after_version or change.before_version or 0,
                    payload={
                        "schema_version": 1,
                        "node_id": node_id,
                        "before_status": before_status,
                        "after_status": after_status,
                        "source": "user",
                        "explanation_available": False,
                        "audit_id": str(audit_id),
                    },
                )
                return change

    # ---------------- 总结投影（§10.6 / §16.3） ----------------

    async def apply_projection(
        self,
        *,
        operation_id: UUID,
        user_id: UUID,
        command: ProjectSummaryToGraphCommand,
    ) -> ProjectionOutcome:
        now = _now()
        async with self._session_factory() as session:
            async with session.begin():
                registry = KnowledgeGraphRegistry(session)
                if not await registry.node_exists(command.node_id):
                    raise GraphNodeNotFoundError(command.node_id)
                await acquire_user_lock(session, user_id)
                overlays = await gs_repo.lock_overlays(
                    session, user_id=user_id, node_ids=[command.node_id]
                )
                overlay = overlays[0] if overlays else None

                memory_doc = await docs_repo.get_document(
                    session, user_id=user_id, memory_id=command.source_memory_id
                )
                if command.projection_action == "apply_active_version":
                    # source_version 必须仍是活动版本（§10.6）
                    if memory_doc is None or memory_doc["active_version"] != command.source_version:
                        return ProjectionOutcome(changed=False, warning="stale_projection_delivery")
                    evidence = command.evidence
                else:
                    # recompute_without_deleted_version：确认 tombstone 相符（§10.6）
                    if (
                        memory_doc is None
                        or memory_doc["deleted_at"] is None
                        or memory_doc["deleted_version"] != command.source_version
                    ):
                        return ProjectionOutcome(changed=False, warning="tombstone_mismatch")
                    evidence = await self._load_remaining_active_evidence(
                        session,
                        user_id=user_id,
                        node_id=command.node_id,
                        exclude_memory_id=command.source_memory_id,
                    )

                if not evidence:
                    return ProjectionOutcome(changed=False, warning="no_effective_evidence")

                evaluation = evaluate_evidence(evidence, settings=self._settings, now=now)
                if evaluation.status is None:
                    return ProjectionOutcome(changed=False, warning="insufficient_evidence")

                before_status: str | None = overlay["status"] if overlay else None
                before_version: int | None = int(overlay["version"]) if overlay else None
                after_status = evaluation.status
                if after_status == before_status:
                    return ProjectionOutcome(changed=False)

                # grace：用户最近动作内不自动覆盖（§16.3）
                if (
                    overlay is not None
                    and overlay["status_source"] == "user"
                    and overlay["last_user_action_at"] is not None
                ):
                    grace_end = overlay["last_user_action_at"] + timedelta(
                        hours=self._settings.graph_user_action_grace_hours
                    )
                    if now < grace_end:
                        return ProjectionOutcome(changed=False, warning="user_action_grace_active")

                overriding_user = overlay is not None and overlay["status_source"] == "user"
                explanation: str | None = None
                if overriding_user:
                    explanation = self._build_explanation(before_status, after_status, evaluation)

                source_type: Literal["summary_memory", "system_recompute"] = (
                    "summary_memory"
                    if command.projection_action == "apply_active_version"
                    else "system_recompute"
                )
                evidence_snapshot = [
                    {
                        "evidence_ref": e.evidence_ref,
                        "direction": e.direction,
                        "strength": e.strength,
                        "occurred_at": e.occurred_at.isoformat(),
                    }
                    for e in evidence
                ][:50]
                new_version = await gs_repo.upsert_overlay(
                    session,
                    user_id=user_id,
                    node_id=command.node_id,
                    status=after_status,
                    status_source=source_type,
                    source_memory_id=command.source_memory_id,
                    source_memory_version=command.source_version,
                    evidence_snapshot=evidence_snapshot,
                    evidence_count=len(evidence),
                    last_user_action_at=None,
                    last_evidence_at=now,
                )
                audit_id = uuid4()
                await gs_repo.insert_audit(
                    session,
                    audit_id=audit_id,
                    operation_id=operation_id,
                    user_id=user_id,
                    node_id=command.node_id,
                    before_status=before_status,
                    after_status=after_status,
                    before_version=before_version,
                    after_version=new_version,
                    actor_type="summary_projection",
                    reason_codes=evaluation.reason_codes,
                    evidence_refs=evaluation.qualifying_refs,
                    explanation_summary=explanation,
                )
                aggregate_version = new_version
                await outbox_repo.insert_event(
                    session,
                    outbox_id=uuid4(),
                    operation_id=operation_id,
                    user_id=user_id,
                    event_type="graph_state.changed",
                    aggregate_type="graph_node",
                    aggregate_id=command.node_id,
                    aggregate_version=aggregate_version,
                    payload={
                        "schema_version": 1,
                        "node_id": command.node_id,
                        "before_status": before_status,
                        "after_status": after_status,
                        "source": source_type,
                        "explanation_available": explanation is not None,
                        "audit_id": str(audit_id),
                    },
                )
                if explanation is not None:
                    await outbox_repo.insert_event(
                        session,
                        outbox_id=uuid4(),
                        operation_id=operation_id,
                        user_id=user_id,
                        event_type="graph_state.explanation_available",
                        aggregate_type="graph_node",
                        aggregate_id=command.node_id,
                        aggregate_version=aggregate_version,
                        payload={
                            "schema_version": 1,
                            "node_id": command.node_id,
                            "audit_id": str(audit_id),
                            "summary": explanation,
                            "changed_at": now.isoformat(),
                        },
                    )
                return ProjectionOutcome(
                    changed=True,
                    change=GraphStateChangeView(
                        node_id=command.node_id,
                        before_status=before_status,  # type: ignore[arg-type]
                        after_status=after_status,
                        before_version=before_version,
                        after_version=new_version,
                        source_type=source_type,
                        reason_codes=evaluation.reason_codes,
                        changed_at=now,
                    ),
                )

    async def _load_remaining_active_evidence(
        self,
        session: AsyncSession,
        *,
        user_id: UUID,
        node_id: str,
        exclude_memory_id: str,
    ) -> list[GraphProjectionEvidence]:
        """从该节点仍有效的其他活动总结记忆重新聚合证据（§10.6）。"""
        links = await gs_repo.list_active_links_for_node(session, user_id=user_id, node_id=node_id)
        evidence: list[GraphProjectionEvidence] = []
        for link in links:
            if link["memory_id"] == exclude_memory_id:
                continue
            result = await session.execute(
                text(
                    """
                    SELECT evidence_refs, created_at FROM memory_commits
                    WHERE user_id = :user_id AND memory_id = :memory_id
                      AND after_version = :version
                    ORDER BY created_at DESC LIMIT 1
                    """
                ),
                {
                    "user_id": user_id,
                    "memory_id": link["memory_id"],
                    "version": link["memory_version"],
                },
            )
            row = result.mappings().first()
            if not row:
                continue
            for ref in row["evidence_refs"] or []:
                evidence.append(
                    GraphProjectionEvidence(
                        evidence_ref=str(ref),
                        direction="learning",
                        strength=0.6,
                        occurred_at=row["created_at"],
                    )
                )
        return evidence

    @staticmethod
    def _build_explanation(before: str | None, after: str, evaluation: EvidenceEvaluation) -> str:
        label = {"learning": "学习中", "proficient": "熟练", "expert": "精通"}
        before_label = label.get(before or "", "无状态")
        return (f"系统根据新的学习证据将该节点从{before_label}调整为{label[after]}。")[:500]

    # ---------------- 推荐（§16.5） ----------------

    async def recommendations(self, *, user_id: UUID, limit: int = 20) -> list[GraphRecommendation]:
        """确定性推荐排序，不调用模型。"""
        async with self._session_factory() as session:
            registry = KnowledgeGraphRegistry(session)
            nodes = await registry.list_nodes()
            edges = await registry.list_edges()
            overlays = await gs_repo.list_overlays(session, user_id=user_id)
            activity = await gs_repo.list_node_activity(session, user_id=user_id)

        status_by_node = {o["node_id"]: o for o in overlays}
        activity_by_node = {a["node_id"]: a for a in activity}
        prereq_map: dict[str, list[str]] = {}
        successors_map: dict[str, list[str]] = {}
        for edge in edges:
            prereq_map.setdefault(str(edge["to_node_id"]), []).append(str(edge["from_node_id"]))
            successors_map.setdefault(str(edge["from_node_id"]), []).append(str(edge["to_node_id"]))
        title_by_node = {str(n["node_id"]): str(n["title"]) for n in nodes}

        async def related_memories(node_id: str) -> list[str]:
            async with self._session_factory() as session:
                links = await gs_repo.list_active_links_for_node(
                    session, user_id=user_id, node_id=node_id
                )
                return sorted({str(link["memory_id"]) for link in links})

        scored: list[tuple[int, str, GraphRecommendation]] = []
        for node in nodes:
            node_id = str(node["node_id"])
            overlay = status_by_node.get(node_id)
            status: str | None = overlay["status"] if overlay else None
            act = activity_by_node.get(node_id)
            reasons: list[str] = []
            rank: int | None = None

            has_recent_activity = act is not None and (
                act["last_viewed_at"] is not None or act["last_check_in_at"] is not None
            )
            if status == "learning" and has_recent_activity:
                rank = 1
                reasons.append("CONTINUE_LEARNING")
            elif status == "learning":
                # 当前学习节点缺失的前置在下一轮处理；learning 本身也继续推荐
                rank = 1
                reasons.append("CONTINUE_LEARNING")

            if rank is None and status is None:
                # 已熟练节点直接后继且无状态
                prereqs = prereq_map.get(node_id, [])
                if any(status_by_node.get(p, {}).get("status") == "proficient" for p in prereqs):
                    rank = 3
                    reasons.append("NEXT_GRAPH_NODE")
                # 当前学习节点缺失的前置
                if any(
                    status_by_node.get(s, {}).get("status") == "learning"
                    for s in successors_map.get(node_id, [])
                ):
                    rank = 2
                    reasons.append("PREREQUISITE_GAP")

            if rank is None and status == "proficient":
                updated = overlay["updated_at"] if overlay else None
                if updated and (_now() - updated) > timedelta(days=30):
                    rank = 5
                    reasons.append("STALE_PROFICIENCY")

            if rank is None and status == "expert":
                continue  # expert 默认不推荐

            if rank is None:
                continue

            related = await related_memories(node_id)
            if related and "SUMMARY_MEMORY_SIGNAL" not in reasons:
                reasons.append("SUMMARY_MEMORY_SIGNAL")
            scored.append(
                (
                    rank,
                    node_id,
                    GraphRecommendation(
                        node_id=node_id,
                        title=title_by_node.get(node_id, node_id),
                        status=status,  # type: ignore[arg-type]
                        reason_codes=reasons,  # type: ignore[arg-type]
                        prerequisite_node_ids=sorted(prereq_map.get(node_id, [])),
                        related_memory_ids=related,
                        updated_at=overlay["updated_at"] if overlay else None,
                    ),
                )
            )
        scored.sort(key=lambda item: (item[0], item[1]))
        return [item[2] for item in scored[:limit]]
