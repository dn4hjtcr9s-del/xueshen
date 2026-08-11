"""图谱推荐服务（规格 §16.5）。

不调用模型：固定图谱 + 用户 Overlay + 已确认弱连接的确定性排序。
排序读取 graph_user_node_activity 的 exposure/近期活跃信号，只影响排序，
不改变状态（§16.5）；related_memory_ids 只从 memory_graph_links 读取
（active=true 且版本等于当前活动 mastery 版本，§13.8.1）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.graph_state import GraphRecommendation
from backend.memory.knowledge_graph.registry import KnowledgeGraphRegistry
from backend.memory.persistence import graph_states as graph_repo
from backend.memory.services.memory_service import MemoryService
from backend.settings import Settings

#: 「最近有学习活动」窗口（§16.5 优先级 1；规格未给天数，取 30 天）
RECENT_ACTIVITY_DAYS = 30
#: 「长期未复习」窗口（§16.5 优先级 5；规格未给天数，取 30 天）
STALE_PROFICIENCY_DAYS = 30

_REASON_CONTINUE = "CONTINUE_LEARNING"
_REASON_GAP = "PREREQUISITE_GAP"
_REASON_NEXT = "NEXT_GRAPH_NODE"
_REASON_CONFLICT = "REVIEW_AFTER_CONFLICT"
_REASON_SUMMARY = "SUMMARY_MEMORY_SIGNAL"
_REASON_STALE = "STALE_PROFICIENCY"


def _latest_activity_at(activity: dict[str, Any] | None) -> datetime | None:
    if activity is None:
        return None
    candidates = [
        activity.get("last_viewed_at"),
        activity.get("last_bookmarked_at"),
        activity.get("last_check_in_at"),
    ]
    present = [ts for ts in candidates if ts is not None]
    return max(present) if present else None


def _age_days(ts: datetime, now: datetime) -> float:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (now - ts).total_seconds() / 86400


def has_strong_conflict(overlay: dict[str, Any], *, threshold: float) -> bool:
    """evidence_snapshot 中存在达到强冲突阈值的证据（§16.5 优先级 6 / §16.3）。"""
    for entry in overlay.get("evidence_snapshot") or []:
        if (
            isinstance(entry, dict)
            and entry.get("direction") == "conflict"
            and float(entry.get("strength") or 0.0) >= threshold
        ):
            return True
    return False


def rank_recommendations(
    *,
    node_ids: list[str],
    edges: list[tuple[str, str]],
    overlays: dict[str, dict[str, Any]],
    activities: dict[str, dict[str, Any]],
    review_signal_node_ids: set[str],
    now: datetime,
    strong_conflict_threshold: float,
) -> list[tuple[str, int, list[str], tuple[float, float, float, str]]]:
    """§16.5 确定性分级排序（纯函数，便于单元测试）。

    edges 为 (from_node_id, to_node_id) prerequisite 边（from 是 to 的前置）。
    返回 (node_id, bucket, reason_codes, sort_key) 列表，按 sort_key 升序即
    推荐顺序。bucket 即 §16.5 优先级 1–6；expert 无强冲突证据不入列。
    规格未明确的谓词（最小实现，报告逐条标注）：
    - 「最近有学习活动」：三种 activity 时间任一在近 30 天内；
    - 「缺失的前置节点」：无 Overlay（无状态）；
    - 「长期未复习」：proficient 且 overlay 与 activity 最近时间均早于 30 天前；
    - 同 bucket 内按最近 activity 降序、event_count 降序、node_id 升序。
    """
    learning_nodes = {
        node_id for node_id, overlay in overlays.items() if overlay.get("status") == "learning"
    }
    proficient_nodes = {
        node_id for node_id, overlay in overlays.items() if overlay.get("status") == "proficient"
    }
    prerequisite_of_learning = {
        from_node for from_node, to_node in edges if to_node in learning_nodes
    }
    successor_of_proficient = {
        to_node for from_node, to_node in edges if from_node in proficient_nodes
    }
    ranked: list[tuple[str, int, list[str], tuple[float, float, float, str]]] = []
    for node_id in node_ids:
        overlay = overlays.get(node_id)
        status = overlay.get("status") if overlay else None
        activity = activities.get(node_id)
        last_activity = _latest_activity_at(activity)
        reasons: list[tuple[int, str]] = []
        if status == "expert":
            # §16.5 优先级 6：expert 默认不推荐，除非存在新的强冲突证据
            if overlay is not None and has_strong_conflict(
                overlay, threshold=strong_conflict_threshold
            ):
                reasons.append((6, _REASON_CONFLICT))
        else:
            if (
                status == "learning"
                and last_activity is not None
                and _age_days(last_activity, now) <= RECENT_ACTIVITY_DAYS
            ):
                reasons.append((1, _REASON_CONTINUE))
            if status is None and node_id in prerequisite_of_learning:
                reasons.append((2, _REASON_GAP))
            if status is None and node_id in successor_of_proficient:
                reasons.append((3, _REASON_NEXT))
            if node_id in review_signal_node_ids:
                reasons.append((4, _REASON_SUMMARY))
            if status == "proficient" and overlay is not None:
                references = [overlay["updated_at"]] if overlay.get("updated_at") else []
                if last_activity is not None:
                    references.append(last_activity)
                if references and min(_age_days(ts, now) for ts in references) > (
                    STALE_PROFICIENCY_DAYS
                ):
                    reasons.append((5, _REASON_STALE))
        if not reasons:
            continue
        bucket = min(bucket for bucket, _code in reasons)
        codes = [code for _bucket, code in sorted(reasons)]
        sort_key = (
            float(bucket),
            -(last_activity.timestamp()) if last_activity is not None else 0.0,
            -float(activity.get("event_count") or 0) if activity else 0.0,
            node_id,
        )
        ranked.append((node_id, bucket, codes, sort_key))
    ranked.sort(key=lambda item: item[3])
    return ranked


class RecommendationService:
    """§16.5 推荐：注册表 + Overlay + activity + 弱连接的只读组装。"""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        memory_service: MemoryService,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._memory_service = memory_service

    async def recommend(
        self, *, user_id: UUID, now: datetime | None = None
    ) -> list[tuple[GraphRecommendation, list[Any]]]:
        """返回 (GraphRecommendation, cursor sort_key) 全量有序列表，由路由分页。"""
        now = now or datetime.now(UTC)
        async with self._session_factory() as session:
            registry = KnowledgeGraphRegistry(session)
            nodes = await registry.list_nodes()
            edges = await registry.list_edges()
            overlay_rows = await graph_repo.list_overlays(session, user_id=user_id)
            activity_rows = await graph_repo.list_node_activity(session, user_id=user_id)
            links = await graph_repo.list_current_active_links(session, user_id=user_id)

        prerequisites_by_node: dict[str, list[str]] = {}
        edge_pairs: list[tuple[str, str]] = []
        for edge in edges:
            from_node = str(edge["from_node_id"])
            to_node = str(edge["to_node_id"])
            prerequisites_by_node.setdefault(to_node, []).append(from_node)
            edge_pairs.append((from_node, to_node))
        related_by_node: dict[str, list[str]] = {}
        linked_memory_ids: dict[str, list[str]] = {}
        for link in links:
            node_id = str(link["node_id"])
            memory_id = str(link["memory_id"])
            related_by_node.setdefault(node_id, []).append(memory_id)
            linked_memory_ids.setdefault(memory_id, []).append(node_id)

        # 优先级 4：总结记忆明确建议复习（review_advice 非空）且可靠映射的节点
        review_signal_node_ids: set[str] = set()
        for memory_id, node_ids in linked_memory_ids.items():
            if not memory_id.startswith("mastery:"):
                continue
            doc = await self._memory_service.get_mastery(
                user_id=user_id, topic_key=memory_id.removeprefix("mastery:")
            )
            if doc is not None and doc.review_advice:
                review_signal_node_ids.update(node_ids)

        overlays = {str(row["node_id"]): row for row in overlay_rows}
        activities = {str(row["node_id"]): row for row in activity_rows}
        titles = {str(node["node_id"]): str(node["title"]) for node in nodes}
        ranked = rank_recommendations(
            node_ids=sorted(titles),
            edges=edge_pairs,
            overlays=overlays,
            activities=activities,
            review_signal_node_ids=review_signal_node_ids,
            now=now,
            strong_conflict_threshold=self._settings.graph_strong_conflict_strength,
        )
        recommendations: list[tuple[GraphRecommendation, list[Any]]] = []
        for node_id, _bucket, codes, sort_key in ranked:
            overlay = overlays.get(node_id)
            activity = activities.get(node_id)
            updated_at: datetime | None = None
            if overlay is not None and overlay.get("updated_at") is not None:
                updated_at = overlay["updated_at"]
            elif activity is not None:
                updated_at = activity.get("updated_at")
            recommendations.append(
                (
                    GraphRecommendation(
                        node_id=node_id,
                        title=titles[node_id],
                        status=overlay.get("status") if overlay else None,
                        reason_codes=codes,  # type: ignore[arg-type]
                        prerequisite_node_ids=sorted(prerequisites_by_node.get(node_id, [])),
                        related_memory_ids=sorted(set(related_by_node.get(node_id, []))),
                        updated_at=updated_at,
                    ),
                    list(sort_key),
                )
            )
        return recommendations
