"""Study Plan Generation Graph（§9.2/§14/§16，v1.2）。

节点顺序：validate_confirmed_intent → load_memory_context → generate_blueprint
→ deterministic_schedule_and_persist。模型只产出内容蓝图；日期、预算、
拆分与落库全部由确定性引擎完成（§9.2）。Memory 不可用 → 通用模板蓝图 +
personalization_status=degraded（§16）。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, TypedDict
from uuid import UUID

from langgraph.graph import END, StateGraph

from backend.settings import Settings
from backend.study.contracts.errors import StudyPlanGenerationFailedError
from backend.study.contracts.graph import PlanBlueprint, PlanGenerationState
from backend.study.gateways.memory import StudyMemoryGateway, context_hash
from backend.study.gateways.openai import StudyOpenAIGateway
from backend.study.persistence import repositories as repo
from backend.study.services import plan_service

PLAN_PROMPT_VERSION = "plan-v1"

PLAN_SYSTEM_PROMPT = """你是数学学习计划设计助手。根据学习目标、时间约束与后端提供的
候选知识点，生成结构化任务蓝图。规则：
1. 任务标题具体可执行，禁止模糊表述；
2. topic_key 与 graph_node_id 只能从后端提供的候选列表选择，禁止编造；
3. estimated_minutes 是估算值，最终由确定性排期引擎归一化；
4. 学习内容与复习任务数量保持均衡。"""


FEED_PROMPT_VERSION = "feed-v1"

FEED_SYSTEM_PROMPT = """你是数学学习每日推荐助手。根据用户正式任务、长期记忆与知识图谱
候选推荐，生成最多两条自适应推荐任务卡。规则：
1. 推荐标题具体、可执行，理由用一句中文说明；
2. topic_key 与 graph_node_id 只能从后端提供的候选列表选择，禁止编造；
3. estimated_minutes 考虑当天剩余时间，不压过用户正式计划；
4. 没有合适候选时 recommendations 返回空数组。"""


def build_daily_feed_graph(
    *,
    settings: Settings,
    session_factory: Any,
    openai_gateway: StudyOpenAIGateway | None,
    memory_gateway: StudyMemoryGateway | None,
    logger: logging.Logger | None = None,
) -> Any:
    """Daily Feed Graph（§9.3）：正式任务 + 最多两条自适应推荐。"""
    from datetime import date as _date

    from langgraph.graph import END, StateGraph

    from backend.study.contracts.graph import DailyFeedBlueprint

    logger = logger or logging.getLogger("study.graph.daily_feed")

    class FeedState(TypedDict, total=False):
        user_id: str
        operation_id: str
        feed_run_id: str
        plan_id: str
        revision_id: str | None
        local_date: str
        timezone: str
        memory_context: dict[str, Any] | None
        formal_items: list[dict[str, Any]]
        recommendation_items: list[dict[str, Any]]

    async def read_inputs(state: FeedState) -> FeedState:
        state["formal_items"] = []
        state["recommendation_items"] = []
        return state

    async def load_memory(state: FeedState) -> FeedState:
        memory_context: dict[str, Any] | None = None
        if settings.study_daily_feed_enabled and settings.study_memory_read_enabled:
            if memory_gateway is None:
                raise StudyPlanGenerationFailedError("Memory Gateway 未装配（daily feed 已开启）")
            try:
                from backend.study.gateways.memory import FEED_MEMORY_QUERY

                memory_context = await memory_gateway.read_context(query=FEED_MEMORY_QUERY)
            except Exception as exc:
                logger.warning("Daily feed Memory 不可用，仅展示正式任务: %s", type(exc).__name__)
                memory_context = None
        state["memory_context"] = memory_context
        return state

    async def generate(state: FeedState) -> FeedState:

        candidates = _candidate_topics(state.get("memory_context"))
        recommendations: list[Any] = []
        if settings.study_daily_feed_enabled and candidates and openai_gateway is not None:
            async with session_factory() as session:
                blueprint = await openai_gateway.structured_call(
                    session=session,
                    user_id=UUID(state["user_id"]),
                    operation_id=UUID(state["operation_id"]),
                    purpose="feed",
                    prompt_version=FEED_PROMPT_VERSION,
                    system_prompt=FEED_SYSTEM_PROMPT,
                    user_payload={
                        "local_date": state["local_date"],
                        "candidate_topics": candidates[:6],
                    },
                    text_format=DailyFeedBlueprint,
                    cache_retention_days=settings.study_model_response_cache_retention_days,
                    now=datetime.now(UTC),
                )
                await session.commit()
            for rec in blueprint.recommendations:
                if rec.graph_node_id and rec.graph_node_id not in {
                    c["graph_node_id"] for c in candidates if c.get("graph_node_id")
                }:
                    raise StudyPlanGenerationFailedError(
                        f"feed 输出候选集外 graph_node_id: {rec.graph_node_id}"
                    )
                recommendations.append(
                    {
                        "source_type": "recommendation",
                        "task_id": None,
                        "topic_key": rec.topic_key,
                        "graph_node_id": rec.graph_node_id,
                        "title": rec.title,
                        "reason": "今日自适应推荐（Memory/图谱信号）",
                        "reason_codes": ["NEXT_GRAPH_NODE"],
                        "estimated_minutes": rec.estimated_minutes,
                        "launch_payload": {
                            "topic_key": rec.topic_key,
                            "graph_node_id": rec.graph_node_id,
                        },
                    }
                )
        state["recommendation_items"] = recommendations
        return state

    async def persist(state: FeedState) -> FeedState:
        from backend.study.services import feed_service as fs

        async with session_factory() as session:
            formal = await repo.list_tasks_for_date(
                session,
                plan_id=UUID(state["plan_id"]),
                scheduled_date=_date.fromisoformat(state["local_date"]),
            )
            items = [
                {
                    "source_type": "formal_task",
                    "task_id": t["task_id"],
                    "topic_key": t["topic_key"],
                    "graph_node_id": t["graph_node_id"],
                    "title": t["title"],
                    "reason": "今日正式任务",
                    "reason_codes": [],
                    "estimated_minutes": t["estimated_minutes"],
                    "launch_payload": {
                        "task_id": str(t["task_id"]),
                        "topic_key": t["topic_key"],
                        "graph_node_id": t["graph_node_id"],
                    },
                }
                for t in formal
            ]
            items.extend(state.get("recommendation_items", []))
            await fs.persist_feed_result(
                session,
                feed_run_id=UUID(state["feed_run_id"]),
                items=items,
                now=datetime.now().astimezone(),
                memory_context_hash=context_hash(state.get("memory_context")),
            )
        return state

    graph = StateGraph(FeedState)
    graph.add_node("read_inputs", read_inputs)
    graph.add_node("load_memory", load_memory)
    graph.add_node("generate", generate)
    graph.add_node("persist", persist)
    graph.set_entry_point("read_inputs")
    graph.add_edge("read_inputs", "load_memory")
    graph.add_edge("load_memory", "generate")
    graph.add_edge("generate", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


def build_plan_generation_graph(
    *,
    settings: Settings,
    session_factory: Any,
    openai_gateway: StudyOpenAIGateway | None,
    memory_gateway: StudyMemoryGateway | None,
    logger: logging.Logger | None = None,
) -> Any:
    """组装 Plan Generation StateGraph（checkpointer 由 worker 注入）。"""
    logger = logger or logging.getLogger("study.graph.plan_generation")

    async def validate_node(state: PlanGenerationState) -> PlanGenerationState:
        from backend.study.contracts.api import PlanIntent

        intent = PlanIntent.model_validate(state["intent"])
        state["intent"] = intent.model_dump(mode="json")
        return state

    async def load_memory_node(state: PlanGenerationState) -> PlanGenerationState:
        """§14：Memory 只作为模型数据；不可用 → degraded，不失败整个 operation。"""
        memory_context: dict[str, Any] | None = None
        personalization = "not_requested"
        reason: str | None = None
        if settings.study_memory_read_enabled and memory_gateway is not None:
            try:
                memory_context = await memory_gateway.read_context(
                    query=state["intent"].get("goal", ""),
                    token_budget=settings.memory_context_token_budget,
                )
                personalization = "personalized"
            except Exception as exc:
                logger.warning("Memory 不可用，生成降级计划: %s", type(exc).__name__)
                personalization = "degraded"
                reason = f"memory_unavailable:{type(exc).__name__}"
        else:
            personalization = "not_requested"
        state["memory_context"] = memory_context
        state["personalization_status"] = personalization
        state["personalization_reason"] = reason
        return state

    async def generate_blueprint_node(state: PlanGenerationState) -> PlanGenerationState:
        """模型生成蓝图（§9.2）；候选 topic 来自 Memory 图谱推荐（§14.2/§14.3）。"""
        candidates = _candidate_topics(state.get("memory_context"))
        if state["personalization_status"] == "personalized" and candidates:
            if openai_gateway is None:
                raise StudyPlanGenerationFailedError("OpenAI Gateway 未装配")
            async with session_factory() as session:
                blueprint = await openai_gateway.structured_call(
                    session=session,
                    user_id=UUID(state["user_id"]),
                    operation_id=UUID(state["operation_id"]),
                    purpose="plan",
                    prompt_version=PLAN_PROMPT_VERSION,
                    system_prompt=PLAN_SYSTEM_PROMPT,
                    user_payload={
                        "intent": state["intent"],
                        "candidate_topics": candidates,
                    },
                    text_format=PlanBlueprint,
                    cache_retention_days=settings.study_model_response_cache_retention_days,
                    now=datetime.now(UTC),
                )
                await session.commit()
        else:
            # §16：Memory 不可用 → 通用模板蓝图（degraded），不调用模型
            blueprint = PlanBlueprint(
                stages=["通用学习阶段"],
                tasks=[_template_task(state["intent"], index, settings) for index in range(3)],
            )
        # §14.3：候选集外节点直接拒绝（模型输出未知 graph_node_id 时拒绝）
        for task in blueprint.tasks:
            if task.graph_node_id and task.graph_node_id not in {
                c["graph_node_id"] for c in candidates if c.get("graph_node_id")
            }:
                raise StudyPlanGenerationFailedError(
                    f"模型输出候选集外的 graph_node_id: {task.graph_node_id}"
                )
        state["blueprint"] = blueprint.model_dump(mode="json")
        return state

    async def persist_node(state: PlanGenerationState) -> PlanGenerationState:
        """确定性排期 + 落库（复用 Phase 1 引擎，§9.2 最后一段）。"""
        from backend.study.contracts.api import PlanIntent

        intent = PlanIntent.model_validate(state["intent"])
        blueprint = PlanBlueprint.model_validate(state["blueprint"])
        personalization = str(state.get("personalization_status") or "not_requested")
        async with session_factory() as session:
            plan = await plan_service.persist_plan_from_blueprint(
                session,
                user_id=UUID(state["user_id"]),
                intent=intent,
                blueprints=[
                    (t.title, str(t.task_type), t.estimated_minutes, t.topic_key, t.description)
                    for t in blueprint.tasks
                ],
                generation_mode="ai",
                personalization_status=personalization,
                personalization_reason=state.get("personalization_reason"),
                change_summary="AI 生成的初始计划草案（§9.2）",
                memory_context_hash=context_hash(state.get("memory_context")),
                model_name=(
                    openai_gateway.model_for("plan") if openai_gateway is not None else None
                ),
                prompt_version=PLAN_PROMPT_VERSION if personalization == "personalized" else None,
            )
            await session.commit()
        state["plan_id"] = str(plan["plan_id"])
        state["revision_id"] = str(plan["current_revision_id"])
        return state

    graph = StateGraph(PlanGenerationState)
    graph.add_node("validate", validate_node)
    graph.add_node("load_memory", load_memory_node)
    graph.add_node("generate_blueprint", generate_blueprint_node)
    graph.add_node("persist", persist_node)
    graph.set_entry_point("validate")
    graph.add_edge("validate", "load_memory")
    graph.add_edge("load_memory", "generate_blueprint")
    graph.add_edge("generate_blueprint", "persist")
    graph.add_edge("persist", END)
    return graph.compile()


def _candidate_topics(memory_context: dict[str, Any] | None) -> list[dict[str, Any]]:
    """从 Memory context 提取候选知识点（§14.2：模型只能从候选集合选择）。"""
    if not memory_context:
        return []
    recommendations = memory_context.get("recommendations") or []
    candidates: list[dict[str, Any]] = []
    for item in recommendations:
        if isinstance(item, dict):
            candidates.append(
                {
                    "topic_key": item.get("topic_key"),
                    "graph_node_id": item.get("graph_node_id"),
                }
            )
    return candidates


def _template_task(intent: dict[str, Any], index: int, settings: Settings) -> Any:
    """§16 降级模板任务：按目标生成确定性的通用阶段任务（不调用模型）。"""
    from backend.study.contracts.graph import BlueprintTask

    titles = [
        "建立目标章节的整体框架",
        "逐个攻破目标章节的核心概念",
        "完成一次目标章节的综合回顾",
    ]
    kinds = ["learn", "learn", "review"]
    minutes = min(
        max(intent.get("session_min_minutes", 20), 30),
        intent.get("session_max_minutes", 60),
    )
    return BlueprintTask(
        title=f"{titles[index]}（{intent.get('goal', '学习目标')[:30]}）",
        task_type=kinds[index],  # type: ignore[arg-type]
        estimated_minutes=minutes,
        topic_key=None,
        graph_node_id=None,
        description="Memory 不可用时的通用降级蓝图（§16）",
    )
