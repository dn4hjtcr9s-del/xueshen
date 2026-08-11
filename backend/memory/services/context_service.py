"""学习上下文组装服务（规格 §12.4 / §12.5）。

- token 估算（确定性近似，不引入分词器依赖）：非 ASCII 字符（中文等）
  每字符计 1 token，ASCII 字符每 4 个计 1 token（向上取整），按注入字段求和。
- 优先级（§12.4）：精确相关 mastery → learner 目标/计划/偏好 →
  相关图谱状态与推荐原因 → 其他弱相关总结记忆。
- 超预算裁剪（§12.4）：先删低优先级文档 → evidence 只保留前 10 条 ref →
  压缩建议复习与概况（整句/整字段粒度，绝不截断单条事实到语义不完整）。
- 总结记忆与图谱 Overlay 只在组装阶段弱融合，不合并存储（§12.4 末段）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.memory.contracts.context import (
    LearningContext,
    LearningContextGraphState,
    LearningContextLearner,
    LearningContextMastery,
    LearningContextRequest,
    LearningContextTokenUsage,
)
from backend.memory.contracts.errors import InvalidPayloadError
from backend.memory.contracts.graph_state import GraphRecommendation
from backend.memory.persistence import graph_states as graph_repo
from backend.memory.persistence import index_entries as index_repo
from backend.memory.services.memory_service import MemoryService
from backend.memory.services.recommendation_service import RecommendationService
from backend.memory.services.search_service import (
    MAX_CANDIDATES,
    SIMILARITY_THRESHOLD,
    normalize_search_query,
    score_candidate,
)
from backend.memory.storage.markdown_schema import LearnerDocument, MasteryDocument
from backend.settings import Settings

#: 上下文内注入的推荐条数上限（§12.4 优先级 3「推荐原因」；规格未给数量）
CONTEXT_RECOMMENDATION_LIMIT = 5
#: 裁剪级别 2：每条记忆保留的 evidence ref 数（§12.4「只保留 evidence ref」）
TRIMMED_EVIDENCE_REF_LIMIT = 10


def estimate_tokens(text: str) -> int:
    """确定性 token 近似：中文等非 ASCII 每字符 1，ASCII 每 4 字符 1。"""
    ascii_count = sum(1 for char in text if ord(char) < 128)
    return (ascii_count + 3) // 4 + (len(text) - ascii_count)


def estimate_tokens_all(parts: list[str]) -> int:
    return sum(estimate_tokens(part) for part in parts)


def _learner_tokens(learner: LearningContextLearner) -> int:
    return estimate_tokens_all(learner.preferences + learner.goals + learner.plans)


def _mastery_tokens(mastery: LearningContextMastery) -> int:
    return (
        estimate_tokens(mastery.title)
        + estimate_tokens(mastery.overview)
        + estimate_tokens_all(mastery.understood + mastery.difficulties + mastery.review_advice)
    )


def _graph_state_tokens(state: LearningContextGraphState) -> int:
    return estimate_tokens(state.title) + estimate_tokens_all(state.reason_codes)


def _recommendation_tokens(recommendation: GraphRecommendation) -> int:
    return estimate_tokens(recommendation.title) + estimate_tokens_all(
        list(recommendation.reason_codes) + recommendation.related_memory_ids
    )


def _total_tokens(context: LearningContext) -> int:
    total = 0
    if context.learner is not None:
        total += _learner_tokens(context.learner)
    total += sum(_mastery_tokens(m) for m in context.mastery)
    total += sum(_graph_state_tokens(s) for s in context.graph_states)
    total += sum(_recommendation_tokens(r) for r in context.recommendations)
    return total


def _first_sentence(text: str) -> str:
    """压缩为第一个完整句（整句粒度，不截断单条事实，§12.4 裁剪级别 3/4）。"""
    for delimiter in ("。", "！", "？", "\n"):
        index = text.find(delimiter)
        if 0 <= index < len(text) - 1:
            return text[: index + 1]
    return text


def assemble_context(
    *,
    user_id: UUID,
    query: str,
    budget: int,
    learner: LearnerDocument | None,
    exact_mastery: list[MasteryDocument],
    weak_mastery: list[MasteryDocument],
    graph_states: list[LearningContextGraphState],
    recommendations: list[GraphRecommendation],
) -> LearningContext:
    """按 §12.4 优先级组装并按四级规则裁剪（纯函数，便于单元测试）。

    exact_mastery / weak_mastery 调用方已按相关度降序排列；裁剪从尾部
    （最低相关度）开始删除。
    """
    truncated = False

    def _learner_view(doc: LearnerDocument) -> LearningContextLearner:
        return LearningContextLearner(
            preferences=list(doc.preferences),
            goals=list(doc.goals),
            plans=list(doc.plans),
            version=doc.version,
            updated_at=doc.updated_at,
            evidence_refs=list(doc.evidence_refs)[:100],
        )

    def _mastery_view(doc: MasteryDocument) -> LearningContextMastery:
        return LearningContextMastery(
            memory_id=f"mastery:{doc.topic_key}",
            topic_key=doc.topic_key,
            title=doc.topic_title,
            overview=doc.overview,
            understood=list(doc.understood),
            difficulties=list(doc.difficulties),
            review_advice=list(doc.review_advice),
            version=doc.version,
            updated_at=doc.updated_at,
            evidence_refs=list(doc.evidence_refs)[:100],
        )

    context = LearningContext(
        user_id=user_id,
        query=query,
        learner=_learner_view(learner) if learner is not None else None,
        mastery=[_mastery_view(d) for d in (*exact_mastery, *weak_mastery)],
        graph_states=list(graph_states),
        recommendations=list(recommendations),
        token_usage=LearningContextTokenUsage(budget=budget, estimated=0, remaining=budget),
        truncated=False,
    )

    def _fits() -> bool:
        return _total_tokens(context) <= budget

    # 裁剪级别 1：先删除低排序文档（弱相关 mastery，从最低相关度开始）
    weak_count = len(weak_mastery)
    while not _fits() and weak_count > 0:
        context.mastery.pop()
        weak_count -= 1
        truncated = True
    # 裁剪级别 2：移除旧 evidence 详情，只保留 evidence ref
    # （本实现注入内容本就只有 ref，故收缩为每条记忆最多保留前 10 条 ref）
    if not _fits():
        refs_owner: list[Any] = list(context.mastery)
        if context.learner is not None:
            refs_owner.append(context.learner)
        for item in refs_owner:
            if len(item.evidence_refs) > TRIMMED_EVIDENCE_REF_LIMIT:
                item.evidence_refs = item.evidence_refs[:TRIMMED_EVIDENCE_REF_LIMIT]
                truncated = True
    # 裁剪级别 3：压缩建议复习和历史描述（整句/整字段粒度）
    if not _fits():
        for mastery in context.mastery:
            if mastery.review_advice:
                mastery.review_advice = []
                truncated = True
            compressed = _first_sentence(mastery.overview)
            if compressed != mastery.overview:
                mastery.overview = compressed
                truncated = True
    # 仍超预算：继续按优先级从低到高整体删除（P3 推荐/图谱尾部 → P2 learner → P1 尾部）
    while not _fits() and context.recommendations:
        context.recommendations.pop()
        truncated = True
    while not _fits() and context.graph_states:
        context.graph_states.pop()
        truncated = True
    if not _fits() and context.learner is not None:
        context.learner = None
        truncated = True
    while not _fits() and len(context.mastery) > 1:
        context.mastery.pop()
        truncated = True

    estimated = _total_tokens(context)
    context.token_usage = LearningContextTokenUsage(
        budget=budget, estimated=estimated, remaining=max(0, budget - estimated)
    )
    context.truncated = truncated
    return context


class LearningContextService:
    """§12.4 LearningContextService；依赖注入 settings/session_factory/memory_service。"""

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

    async def build(
        self, *, user_id: UUID, request: LearningContextRequest, now: datetime | None = None
    ) -> LearningContext:
        now = now or datetime.now(UTC)
        budget = (
            request.token_budget
            if request.token_budget is not None
            else self._settings.memory_context_token_budget
        )
        query = normalize_search_query(request.query)
        if not query:
            raise InvalidPayloadError("query 规范化后为空", field="query")
        explicit_topics = set(request.topic_keys)

        async with self._session_factory() as session:
            rows = await index_repo.search_candidates(
                session,
                user_id=user_id,
                query=query,
                topic_keys=sorted(explicit_topics),
                memory_types=["mastery"],
                min_similarity=SIMILARITY_THRESHOLD,
                limit=MAX_CANDIDATES,
            )
        learner = await self._memory_service.get_learner(user_id=user_id)

        scored: list[tuple[float, dict[str, Any], bool]] = []
        for row in rows:
            topic_key = row.get("topic_key")
            title = str(row["title"])
            exact = (
                (topic_key is not None and topic_key in explicit_topics)
                or (topic_key is not None and topic_key == query)
                or normalize_search_query(title) == query
            )
            similarity = float(row["similarity"])
            if not exact and similarity < SIMILARITY_THRESHOLD:
                continue
            score = score_candidate(
                topic_key=topic_key,
                title=title,
                similarity=similarity,
                query=query,
                topic_filter=bool(explicit_topics),
                updated_at=row["updated_at"],
                now=now,
            )
            scored.append((score, row, exact))
        scored.sort(key=lambda item: -item[0])

        exact_docs: list[MasteryDocument] = []
        weak_docs: list[MasteryDocument] = []
        for _score, row, exact in scored:
            topic_key = row.get("topic_key")
            if not topic_key:
                continue
            doc = await self._memory_service.get_mastery(user_id=user_id, topic_key=topic_key)
            if doc is None:
                continue
            (exact_docs if exact else weak_docs).append(doc)

        # 优先级 3：与请求明确相关的图谱状态（精确 mastery 的当前版本弱连接节点）
        graph_states: list[LearningContextGraphState] = []
        seen_nodes: set[str] = set()
        async with self._session_factory() as session:
            from backend.memory.knowledge_graph.registry import KnowledgeGraphRegistry

            registry = KnowledgeGraphRegistry(session)
            for doc in exact_docs:
                links = await graph_repo.list_active_links_for_memory(
                    session,
                    user_id=user_id,
                    memory_id=f"mastery:{doc.topic_key}",
                    active_version=doc.version,
                )
                for link in links:
                    node_id = str(link["node_id"])
                    if node_id in seen_nodes:
                        continue
                    seen_nodes.add(node_id)
                    node = await registry.get_node(node_id)
                    if node is None:
                        continue
                    overlay = await graph_repo.get_overlay(
                        session, user_id=user_id, node_id=node_id
                    )
                    audit = await graph_repo.latest_audit(session, user_id=user_id, node_id=node_id)
                    graph_states.append(
                        LearningContextGraphState(
                            node_id=node_id,
                            title=str(node["title"]),
                            status=overlay.get("status") if overlay else None,
                            reason_codes=(
                                [str(c) for c in (audit.get("reason_codes") or [])] if audit else []
                            ),
                        )
                    )

        recommendation_service = RecommendationService(
            settings=self._settings,
            session_factory=self._session_factory,
            memory_service=self._memory_service,
        )
        ranked = await recommendation_service.recommend(user_id=user_id, now=now)
        recommendations = [item for item, _key in ranked[:CONTEXT_RECOMMENDATION_LIMIT]]

        return assemble_context(
            user_id=user_id,
            query=query,
            budget=budget,
            learner=learner,
            exact_mastery=exact_docs,
            weak_mastery=weak_docs,
            graph_states=graph_states,
            recommendations=recommendations,
        )
