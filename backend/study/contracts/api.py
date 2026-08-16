"""Study API 契约：请求/响应 Pydantic 模型（docs/study-plan-push-implementation-plan.md
v1.2，§8/§12）。

- 所有请求模型 extra="forbid"（§20.4：REQUEST_EXTRA_FIELD 与 INVALID_PAYLOAD 区分）；
- PlanIntent 校验冻结 §8 约束 1–11（缺失信息 → STUDY_PLAN_INPUT_INCOMPLETE 的
  校验语义由路由层转换，模型只负责结构化约束）；
- 响应模型按 §12 冻结形状定义；user_id 只来自 AuthContext，不出现在请求模型（§18.1）。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.study.contracts.domain import (
    ConversationStatus,
    FeedItemStatus,
    FeedRunStatus,
    IntakeStatus,
    OperationStatus,
    PersonalizationStatus,
    PlanStatus,
    RevisionReason,
    RevisionStatus,
    SessionStatus,
    TaskSource,
    TaskStatus,
    TaskType,
)


def _validate_iana_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except Exception as exc:
        raise ValueError(f"timezone 必须是合法 IANA 时区: {value}") from exc
    return value


class WeeklyAvailabilitySlot(BaseModel):
    """每周可学习日（§7.3/§8：ISO 8601，1=周一，7=周日）。"""

    model_config = ConfigDict(extra="forbid")

    day_of_week: int = Field(ge=1, le=7, description="ISO 8601 周几（1=周一）")
    available_minutes: int = Field(ge=0)
    start_local_time: time | None = None
    end_local_time: time | None = None
    is_rest_day: bool = False

    @model_validator(mode="after")
    def _rest_day_rules(self) -> WeeklyAvailabilitySlot:
        if self.is_rest_day:
            if self.available_minutes != 0 or self.start_local_time or self.end_local_time:
                raise ValueError("休息日 available_minutes 必须为 0 且不得提供时段")
        elif self.available_minutes <= 0:
            raise ValueError("非休息日 available_minutes 必须大于 0")
        if (
            self.start_local_time
            and self.end_local_time
            and self.start_local_time >= self.end_local_time
        ):
            raise ValueError("start_local_time 必须早于 end_local_time")
        return self


class PlanIntent(BaseModel):
    """结构化计划意图（§8）。模型禁止自行补造，缺失字段由 intake 追问补齐。"""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=500)
    start_date: date
    target_date: date | None = None
    duration_weeks: int | None = Field(default=None, ge=1, le=52)
    timezone: str
    weekly_availability: list[WeeklyAvailabilitySlot] = Field(min_length=1, max_length=7)
    session_min_minutes: int = Field(ge=5, le=120)
    session_max_minutes: int = Field(ge=5, le=240)
    preferences: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_intent(self) -> PlanIntent:
        _validate_iana_timezone(self.timezone)
        # §8 约束 2：target_date 与 duration_weeks 至少提供一个
        if self.target_date is None and self.duration_weeks is None:
            raise ValueError("target_date 与 duration_weeks 至少提供一个")
        # §8 约束 4：每周总可用时间必须大于零
        if sum(s.available_minutes for s in self.weekly_availability) <= 0:
            raise ValueError("每周总可用时间必须大于零")
        # §8 约束 5
        if self.session_min_minutes > self.session_max_minutes:
            raise ValueError("session_min_minutes 必须小于等于 session_max_minutes")
        # 同一 day_of_week 最多一行（§7.3）
        dows = [s.day_of_week for s in self.weekly_availability]
        if len(dows) != len(set(dows)):
            raise ValueError("weekly_availability 每个 day_of_week 最多一行")
        resolved_target = self.resolved_target_date()
        # §8 约束 6：截止日期必须晚于开始日期
        if resolved_target <= self.start_date:
            raise ValueError("截止日期必须晚于开始日期")
        return self

    def resolved_target_date(self) -> date:
        """target_date 优先；否则 start_date + duration_weeks × 7 天。"""
        if self.target_date is not None:
            return self.target_date
        assert self.duration_weeks is not None
        return self.start_date + timedelta(weeks=self.duration_weeks)

    def weekly_total_minutes(self) -> int:
        return sum(s.available_minutes for s in self.weekly_availability)


class TaskBlueprintItem(BaseModel):
    """用户确认的结构化任务蓝图（§8/D8）。"""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    task_type: TaskType
    estimated_minutes: int = Field(ge=5, le=480)
    topic_key: str | None = None
    description: str = Field(default="", max_length=2000)


class PlanCreateRequest(BaseModel):
    """创建计划请求（§12.2）：manual 直录 / ai 生成两条路径。"""

    model_config = ConfigDict(extra="forbid")

    intent: PlanIntent
    generation_mode: Literal["manual", "ai"]
    task_blueprint: list[TaskBlueprintItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def _mode_rules(self) -> PlanCreateRequest:
        if self.generation_mode == "manual" and not self.task_blueprint:
            raise ValueError("generation_mode=manual 要求 task_blueprint 非空")
        if self.generation_mode == "ai" and self.task_blueprint:
            raise ValueError("generation_mode=ai 不允许携带 task_blueprint")
        return self


class PlanActivateRequest(BaseModel):
    """激活计划请求（§12.2：事务内 CAS + active 唯一约束复查）。"""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class TaskActionRequest(BaseModel):
    """任务写操作通用请求（§12.3：expected_version）。"""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class TaskRescheduleRequest(BaseModel):
    """单任务改期（§12.3/D11：仅 pending，只改自身日期）。"""

    model_config = ConfigDict(extra="forbid")

    scheduled_date: date
    expected_version: int = Field(ge=1)


class RevisionDecisionRequest(BaseModel):
    """proposed revision 的 accept/reject 决策（§12.2/D21）。"""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=500)


class HeartbeatRequest(BaseModel):
    """Session heartbeat（§12.5/D28：单调递增 seq）。"""

    model_config = ConfigDict(extra="forbid")

    seq: int = Field(ge=1)


class AccountPurgeRequest(BaseModel):
    """内部账号清理请求（§12.8/D19：system principal）。"""

    model_config = ConfigDict(extra="forbid")

    account_deletion_id: UUID
    user_id: UUID
    requested_at: datetime


# ---------------------------------------------------------------------------
# 响应模型（§12）
# ---------------------------------------------------------------------------


class AvailabilityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    day_of_week: int
    available_minutes: int
    start_local_time: time | None
    end_local_time: time | None
    is_rest_day: bool


class RevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    revision_id: UUID
    plan_id: UUID
    revision_no: int
    reason: RevisionReason
    status: RevisionStatus
    personalization_status: PersonalizationStatus
    personalization_reason: str | None = None
    proposal_operation_id: UUID | None = None
    base_revision_id: UUID | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    change_summary: str | None = None
    created_at: datetime
    activated_at: datetime | None = None
    decision_at: datetime | None = None
    decision_actor_id: UUID | None = None
    decision_reason: str | None = None


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    plan_id: UUID
    user_id: UUID
    goal: str
    status: PlanStatus
    timezone: str
    start_date: date
    target_date: date
    weekly_minutes: int
    session_min_minutes: int
    session_max_minutes: int
    current_revision_id: UUID | None = None
    personalization_status: PersonalizationStatus | None = None
    version: int
    activated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TaskOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    task_id: UUID
    plan_id: UUID
    revision_id: UUID
    scheduled_date: date
    order_index: int
    task_type: TaskType
    title: str
    description: str
    estimated_minutes: int
    estimation_basis: str
    topic_key: str | None = None
    graph_node_id: str | None = None
    source: TaskSource
    status: TaskStatus
    user_locked: bool
    completion_source: Literal["manual"] | None = None
    completion_suggestion_pending: bool = False
    started_at: datetime | None = None
    completed_at: datetime | None = None
    version: int


class CalendarDayOut(BaseModel):
    local_date: date
    day_of_week: int
    is_rest_day: bool
    available_minutes: int
    planned_minutes: int
    completed_minutes: int
    tasks: list[TaskOut]


class CalendarWeekOut(BaseModel):
    week_index: int
    from_: date = Field(alias="from")
    to: date
    days: list[CalendarDayOut]


class CalendarOut(BaseModel):
    plan_id: UUID
    timezone: str
    start_date: date
    target_date: date
    current_revision_id: UUID | None
    weeks: list[CalendarWeekOut]


class ProgressOut(BaseModel):
    task_progress_percent: int
    workload_progress_percent: int


class HomeActivePlanOut(BaseModel):
    plan_id: UUID
    goal: str
    week_label: str
    personalization_status: PersonalizationStatus | None
    progress_percent: int
    task_progress_percent: int
    workload_progress_percent: int


class HomeTodayOut(BaseModel):
    generation_status: Literal["pending", "ready", "no_active_plan"]
    completed_count: int
    total_count: int
    planned_minutes: int
    tasks: list[TaskOut]
    recommendations: list[dict[str, Any]] = Field(default_factory=list)


class HomeDayOut(BaseModel):
    local_date: date
    active_minutes: int
    completed_task_count: int
    session_count: int


class HomeRecent7DaysOut(BaseModel):
    from_: date = Field(alias="from")
    to: date
    total_active_minutes: int
    days: list[HomeDayOut]


class HomeOut(BaseModel):
    local_date: date
    timezone: str | None
    active_plan: HomeActivePlanOut | None
    today: HomeTodayOut
    recent_7_days: HomeRecent7DaysOut


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    session_id: UUID
    task_id: UUID | None
    conversation_thread_id: str | None = None
    conversation_status: ConversationStatus
    status: SessionStatus
    started_at: datetime
    last_heartbeat_at: datetime | None = None
    last_heartbeat_seq: int
    active_seconds: int
    ended_at: datetime | None = None


class LaunchOut(BaseModel):
    """task launch 稳定响应骨架（§12.5/D24）。"""

    task_id: UUID
    session_id: UUID
    conversation_thread_id: str | None = None
    conversation_status: ConversationStatus
    launch_payload: dict[str, Any]


class OperationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    operation_id: UUID
    user_id: UUID
    operation_type: str
    status: OperationStatus
    error_code: str | None = None
    result: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime


class IdempotentPlanCreateOut(BaseModel):
    plan: PlanOut


class EnsureTodayOut(BaseModel):
    feed_run_id: UUID
    operation_id: UUID
    generation_status: FeedRunStatus = "queued"


class AcceptRecommendationOut(BaseModel):
    feed_item_id: UUID
    task_id: UUID


class IntakeOut(BaseModel):
    """intake 状态（§12.1；Phase 2 接线）。"""

    intake_id: UUID
    status: IntakeStatus
    normalized_intent: dict[str, Any] | None = None
    missing_fields: list[str]
    message_count: int
    version: int
    expires_at: datetime


class FeedItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    feed_item_id: UUID
    feed_run_id: UUID
    source_type: str
    task_id: UUID | None = None
    topic_key: str | None = None
    graph_node_id: str | None = None
    title: str
    reason: str
    reason_codes: list[str]
    estimated_minutes: int | None = None
    status: FeedItemStatus
    expires_at: datetime | None = None
