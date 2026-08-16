"""Study Graph 契约：模型输出 Schema 与图状态（方案 §9/§15.2，v1.2）。

模型输出严格结构化（§18.7）：intake 抽取、计划蓝图、每日推荐都走
Pydantic 校验；未知 graph_node_id / 候选集外 topic 由节点拒绝（§9.2/§14.3）。
"""

from __future__ import annotations

from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from backend.study.contracts.domain import TaskType


class IntakeExtraction(BaseModel):
    """单轮 intake 的结构化抽取（§9.1：严格结构化，禁止模型自行补造）。"""

    model_config = ConfigDict(extra="forbid")

    intent_patch: dict[str, Any] = Field(
        default_factory=dict,
        description="本轮可确认的 PlanIntent 字段（不包含未确认值）",
    )
    missing_fields: list[str] = Field(default_factory=list)
    clarifying_questions: list[str] = Field(default_factory=list)
    ready: bool = False


class BlueprintTask(BaseModel):
    """AI 计划蓝图任务（§9.2：模型只给内容，日期/预算由确定性引擎决定）。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    task_type: TaskType
    estimated_minutes: int = Field(ge=5, le=480)
    topic_key: str | None = None
    graph_node_id: str | None = None
    description: str = Field(default="", max_length=2000)


class PlanBlueprint(BaseModel):
    """AI 生成的完整任务蓝图（§9.2）。"""

    model_config = ConfigDict(extra="forbid")

    stages: list[str] = Field(default_factory=list)
    tasks: list[BlueprintTask] = Field(min_length=1, max_length=120)


class DailyFeedBlueprint(BaseModel):
    """每日推荐蓝图（§9.3/§11.2：最多两条额外推荐）。"""

    model_config = ConfigDict(extra="forbid")

    recommendations: list[BlueprintTask] = Field(default_factory=list, max_length=2)


class IntakeState(TypedDict, total=False):
    """Intake 图状态（§9.1：checkpoint 可恢复）。"""

    user_id: str
    intake_id: str
    messages: list[dict[str, str]]
    normalized_intent: dict[str, Any]
    missing_fields: list[str]
    extraction: dict[str, Any]
    status: str


class PlanGenerationState(TypedDict, total=False):
    """Plan Generation 图状态（§9.2）。"""

    user_id: str
    operation_id: str
    intent: dict[str, Any]
    memory_context: dict[str, Any] | None
    personalization_status: str
    personalization_reason: str | None
    blueprint: dict[str, Any] | None
    plan_id: str | None
    revision_id: str | None
    error: str | None
