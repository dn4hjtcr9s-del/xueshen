"""学习上下文组装服务（规格 §12.4 / §12.5）。

- token 估算（确定性近似，不引入分词器依赖）：非 ASCII 字符（中文等）
  每字符计 1 token，ASCII 字符每 4 个计 1 token（向上取整），按注入字段求和。
- 优先级（§12.4）：精确相关 mastery → learner 目标/计划/偏好 →
  相关图谱状态与推荐原因 → 其他弱相关总结记忆。
- 超预算裁剪（§12.4）：先删低优先级文档 → evidence 只保留前 10 条 ref →
  压缩建议复习与概况（整句/整字段粒度，绝不截断单条事实到语义不完整）。
- 总结记忆与图谱 Overlay 只在组装阶段弱融合，不合并存储（§12.4 末段）。
- **双源协调（memory-rebuild §3.5②/§5.9）**：图谱注入段不再只是"弱连接展示"，
  而是长期记忆 mastery 与该用户 KG overlay 的协调点——比较两路的版本/时间，
  更新鲜者优先，同时在返回结构里带 ``conflict``/``conflict_reason``/``state_source``
  等可选键，绝不静默丢差异（决议 D 组）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
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

#: audit.reason_codes 里的强冲突标记（图谱 §16.3 降级原因）
_REASON_AFTER_CONFLICT = "REVIEW_AFTER_CONFLICT"


@dataclass
class GraphStateTask:
    """一次"mastery ↔ KG 节点"双源协调的输入（读路径内部结构，非契约）。

    两路各自的"新鲜度"与可比较状态：

    - **KG 侧**：``current_status`` 是该节点 overlay 的当前状态（无 Overlay 为 None），
      ``projected_version`` 是 overlay 已经消化到的 mastery 版本
      （``graph_user_states.source_memory_version``），``updated_at`` 是 overlay 更新时间。
    - **长期记忆侧**：``memory_version`` / ``memory_updated_at`` 取当前上下文选中的、
      链到该节点的 mastery 文档的最新版本与时间；``memory_status`` 是长期记忆当下的
      可比较结论——有 "difficulties" 且没有正向投影证据时为 "learning"，否则沿用最新
      一次投影评估出来的状态，没有可比较证据时为 None。
    - ``conflict_signal``：图谱最新审计的 ``reason_codes`` 含
      ``REVIEW_AFTER_CONFLICT``（§16.3 因强冲突降级）。
    """

    #: 当前上下文里与该节点配对的那条 mastery 记忆 id（``mastery:{topic_key}``）。
    memory_id: str | None
    memory_version: int | None
    memory_updated_at: datetime | None
    memory_status: str | None
    projected_version: int | None
    #: overlay 是由哪条记忆投影出来的（``source_memory_id``）；与当前链接的
    #: master 不一致说明 KG 侧记的是另一条记忆的结论，需要标注。
    projected_memory_id: str | None
    current_status: str | None
    kg_updated_at: datetime | None
    conflict_signal: bool


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


def _coerce_status(value: str | None) -> Literal["learning", "proficient", "expert"] | None:
    """把自由字符串收敛到契约允许的状态字面量；未知值降级为 None。"""
    if value in ("learning", "proficient", "expert"):
        return value  # type: ignore[return-value]
    return None


def _has_positive_evidence(documents: list[MasteryDocument]) -> bool:
    """mastery 文档是否带有正向证据：``understood`` 非空且没有 ``difficulties``。"""
    return any(doc.understood and not doc.difficulties for doc in documents)


def _mastery_status_from_documents(documents: list[MasteryDocument]) -> str | None:
    """长期记忆侧的可比较结论（§3.5②）。

    mastery 文档本身没有状态字段，"用户对这个主题掌握到什么程度"只在两类信息里：
    文档的 ``difficulties``（仍然困难的点）与图谱最近一次根据该记忆评估出的状态。
    这里的规则最小且确定：

    - 该主题仍列着 ``difficulties`` 且没有正向证据时，长期记忆侧的可比较结论是
      ``learning``；
    - 否则返回 None（"长期记忆侧没有可比较结论"），此时不参与状态比对，只做版本比对。

    因此状态比对只发生在 KG overlay 与"记忆侧明示的学习困难"之间，不会凭空替
    长期记忆推断出 proficient/expert（详见交付报告"缺口"一节）。
    """
    if not documents:
        return None
    if not any(doc.difficulties for doc in documents):
        return None
    return None if _has_positive_evidence(documents) else "learning"


def _kg_state_time(overlay: dict[str, Any] | None) -> datetime | None:
    """KG 侧新鲜度时间：overlay 的 ``last_evidence_at``，退回 ``updated_at``。"""
    if overlay is None:
        return None
    for key in ("last_evidence_at", "updated_at"):
        value = overlay.get(key)
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return None


def _memory_state_time(documents: list[MasteryDocument]) -> datetime | None:
    """长期记忆侧新鲜度时间：选中 mastery 文档的最新 ``updated_at``。"""
    times = [
        doc.updated_at if doc.updated_at.tzinfo is not None else doc.updated_at.replace(tzinfo=UTC)
        for doc in documents
    ]
    return max(times) if times else None


def reconcile_graph_states(
    *,
    node_id: str,
    title: str,
    kg_status: str | None,
    kg_projected_version: int | None,
    kg_updated_at: datetime | None,
    memory_version: int | None,
    memory_updated_at: datetime | None,
    memory_status: str | None,
    kg_conflict_signal: bool,
    kg_reason_codes: list[str],
    memory_id: str | None = None,
    kg_projected_memory_id: str | None = None,
) -> LearningContextGraphState:
    """单节点双源协调（纯函数，§3.5②/§5.9；决议 D 组）。

    裁决顺序（"更新鲜者优先 + 并列标注"，绝不静默覆盖）：

    1. 两路都有版本号 → 比版本；记忆侧版本更大时取记忆侧结论，KG 侧更大时取 overlay。
    2. 版本不可比（缺 link/overlay 版本）→ 比时间戳；并列同刻时长期记忆侧优先
       （记忆链路是权威事实源）并在 ``conflict_reason`` 里写明是并列。
    3. 任一侧缺失 → 直接采用存在的一侧，``conflict=False``（这是覆盖缺口，不是冲突）。

    下列任一情形 ``conflict=True`` 并把原因放进 ``conflict_reason``：

    - 两路都给出可比较结论且不一致（在学 vs 熟练/精通）；
    - 两路版本不一致（一方已包含另一方尚未消化的更新）；
    - 版本并列但图谱侧带未解决强冲突标记（``REVIEW_AFTER_CONFLICT``）。

    ``reason_codes`` 保留图谱侧既有内容（既有键含义不变），出现冲突时追加
    ``DUAL_SOURCE_CONFLICT``，便于上游 graph/conversation 读到。
    """
    reason_codes = list(kg_reason_codes)
    conflict = False
    conflict_reason: str | None = None
    state_source: Literal["memory", "kg"]
    status: str | None = kg_status

    if memory_version is None and memory_updated_at is None:
        # 记忆侧没有信息：保持 KG 原值（覆盖缺口不算冲突）
        state_source = "kg"
    elif kg_status is None and kg_projected_version is None and kg_updated_at is None:
        # 图谱侧没有状态：采用记忆侧可比较结论（可能仍为 None）
        state_source = "memory"
        status = memory_status
    elif memory_version is not None and kg_projected_version is not None:
        if memory_version > kg_projected_version:
            state_source = "memory"
            conflict = True
            conflict_reason = f"memory_newer_version:{memory_version}>{kg_projected_version}"
            status = memory_status or status
        elif memory_version < kg_projected_version:
            state_source = "kg"
            conflict = True
            conflict_reason = f"kg_newer_version:{kg_projected_version}>{memory_version}"
            status = kg_status
        else:
            # 版本并列：长期记忆侧优先，并显式标注
            state_source = "memory" if memory_updated_at is not None else "kg"
            status = memory_status or kg_status
            if memory_status is not None and kg_status is not None and memory_status != kg_status:
                conflict = True
                conflict_reason = (
                    f"version_tie:memory={memory_status},kg={kg_status},memory_preferred"
                )
            elif kg_conflict_signal:
                conflict = True
                conflict_reason = "version_tie:kg_unresolved_conflict"
    else:
        memory_time = memory_updated_at
        kg_time = kg_updated_at
        if memory_time is not None and kg_time is not None:
            if memory_time > kg_time:
                state_source = "memory"
                conflict = True
                conflict_reason = "memory_newer_time"
                status = memory_status or status
            elif memory_time < kg_time:
                state_source = "kg"
                conflict = True
                conflict_reason = "kg_newer_time"
                status = kg_status
            else:
                # 并列同刻：长期记忆侧优先并显式标注，不静默覆盖
                state_source = "memory"
                conflict = True
                conflict_reason = "tie_same_time:memory_preferred"
                status = memory_status or status
        elif memory_time is not None:
            state_source = "memory"
            conflict = True
            conflict_reason = "kg_timestamp_missing"
            status = memory_status or status
        else:
            state_source = "kg"
            conflict = True
            conflict_reason = "memory_timestamp_missing"

    if memory_id is not None and kg_projected_memory_id is not None:
        if kg_projected_memory_id != memory_id:
            # KG overlay 是另一条记忆投影出来的：来源不一致，必须标注
            conflict = True
            conflict_reason = f"source_mismatch:kg={kg_projected_memory_id},memory={memory_id}"
    if memory_status is not None and kg_status is not None and memory_status != kg_status:
        # 状态本身不一致：无论谁更新都标注，避免静默丢差异
        conflict = True
        if conflict_reason is None:
            conflict_reason = f"status_mismatch:memory={memory_status},kg={kg_status}"
    if conflict:
        reason_codes.append("DUAL_SOURCE_CONFLICT")
    if kg_conflict_signal and _REASON_AFTER_CONFLICT not in reason_codes:
        reason_codes.append(_REASON_AFTER_CONFLICT)

    return LearningContextGraphState(
        node_id=node_id,
        title=title,
        status=_coerce_status(status),
        reason_codes=reason_codes,
        state_source=state_source,
        conflict=conflict,
        conflict_reason=conflict_reason,
        memory_version=memory_version,
        kg_projected_version=kg_projected_version,
        memory_updated_at=memory_updated_at,
        kg_updated_at=kg_updated_at,
    )


def align_graph_states(
    graph_states: list[LearningContextGraphState],
    *,
    tasks: dict[str, GraphStateTask] | None,
) -> list[LearningContextGraphState]:
    """图谱注入段 → 双源协调结果（§3.5②）。

    ``tasks`` 为 None（调用方未提供 mastery 侧信息）时逐字返回原状态，既有调用方
    与既有测试行为不变；传入时按 :func:`reconcile_graph_states` 逐节点裁决。

    **调用方接入点**：``LearningContextService.build`` 已填好 tasks；任何绕过 build
    自行组装 ``graph_states`` 的调用方，需要把该节点链到的 mastery 文档版本/时间与
    被选中的 overlay 信息整理成 ``GraphStateTask`` 传进来。
    """
    if not tasks:
        return graph_states
    reconciled: list[LearningContextGraphState] = []
    for state in graph_states:
        task = tasks.get(state.node_id)
        if task is None:
            reconciled.append(state)
            continue
        reconciled.append(
            reconcile_graph_states(
                node_id=state.node_id,
                title=state.title,
                kg_status=task.current_status,
                kg_projected_version=task.projected_version,
                kg_updated_at=task.kg_updated_at,
                memory_version=task.memory_version,
                memory_updated_at=task.memory_updated_at,
                memory_status=task.memory_status,
                kg_conflict_signal=task.conflict_signal,
                kg_reason_codes=list(state.reason_codes),
                memory_id=task.memory_id,
                kg_projected_memory_id=task.projected_memory_id,
            )
        )
    return reconciled


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
    graph_state_tasks: dict[str, GraphStateTask] | None = None,
) -> LearningContext:
    """按 §12.4 优先级组装并按四级规则裁剪（纯函数，便于单元测试）。

    exact_mastery / weak_mastery 调用方已按相关度降序排列；裁剪从尾部
    （最低相关度）开始删除。

    ``graph_state_tasks`` 是 §3.5② 的双源协调输入：给了就先把图谱注入段与长期
    记忆 mastery 对齐（:func:`align_graph_states`），没给则原样注入（既有行为）。
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
        graph_states=align_graph_states(list(graph_states), tasks=graph_state_tasks),
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

        # 优先级 3：与请求明确相关的图谱状态（精确 mastery 的当前版本弱连接节点），
        # 并就地做 §3.5② 双源协调：把长期记忆 mastery 的版本/时间与 KG overlay 的
        # 状态/已消化版本配成 GraphStateTask，交给 align_graph_states 裁决。
        graph_states: list[LearningContextGraphState] = []
        graph_state_tasks: dict[str, GraphStateTask] = {}
        memory_docs_by_node: dict[str, list[MasteryDocument]] = {}
        seen_nodes: set[str] = set()
        async with self._session_factory() as session:
            from backend.memory.knowledge_graph.registry import KnowledgeGraphRegistry

            registry = KnowledgeGraphRegistry(session)
            for doc in exact_docs:
                memory_id = f"mastery:{doc.topic_key}"
                links = await graph_repo.list_active_links_for_memory(
                    session,
                    user_id=user_id,
                    memory_id=memory_id,
                    active_version=doc.version,
                )
                for link in links:
                    node_id = str(link["node_id"])
                    memory_docs_by_node.setdefault(node_id, []).append(doc)
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
                    reason_codes = (
                        [str(c) for c in (audit.get("reason_codes") or [])] if audit else []
                    )
                    graph_states.append(
                        LearningContextGraphState(
                            node_id=node_id,
                            title=str(node["title"]),
                            status=overlay.get("status") if overlay else None,
                            reason_codes=reason_codes,
                        )
                    )
                    graph_state_tasks[node_id] = GraphStateTask(
                        memory_id=memory_id,
                        memory_version=None,
                        memory_updated_at=None,
                        memory_status=None,
                        projected_version=(
                            int(overlay["source_memory_version"])
                            if overlay and overlay.get("source_memory_version") is not None
                            else None
                        ),
                        projected_memory_id=(
                            str(overlay["source_memory_id"])
                            if overlay and overlay.get("source_memory_id") is not None
                            else None
                        ),
                        current_status=overlay.get("status") if overlay else None,
                        kg_updated_at=_kg_state_time(overlay),
                        conflict_signal=_REASON_AFTER_CONFLICT in reason_codes,
                    )
        for node_id, task in graph_state_tasks.items():
            documents = memory_docs_by_node.get(node_id, [])
            task.memory_version = max((doc.version for doc in documents), default=None)
            task.memory_updated_at = _memory_state_time(documents)
            task.memory_status = _mastery_status_from_documents(documents)

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
            graph_state_tasks=graph_state_tasks,
        )
