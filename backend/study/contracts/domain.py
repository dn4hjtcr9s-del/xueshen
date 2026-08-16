"""Study 域领域契约：枚举、常量与纯函数（docs/study-plan-push-implementation-plan.md
v1.2，§7/§10/D11/D18/D28）。

本模块只放不依赖数据库与 I/O 的冻结语义；进度、排期与调整分类等
纯函数集中在 services/，便于确定性单元测试（§20.1）。
"""

from __future__ import annotations

from typing import Literal

#: 计划状态（§7.2；D5：每用户最多一个 active）
PlanStatus = Literal["draft", "active", "paused", "completed", "archived"]

#: Intake 状态（§7.1/§12.1）
IntakeStatus = Literal["collecting", "ready", "confirmed", "exhausted", "expired"]

#: Revision 状态（§7.4/D21）
RevisionStatus = Literal["proposed", "active", "rejected", "superseded"]

#: Revision 产生原因（§7.4）
RevisionReason = Literal[
    "initial", "user_adjustment", "weekly_replan", "missed_task", "memory_change"
]

#: 任务类型（§7.5）
TaskType = Literal["learn", "practice", "review", "assessment"]

#: 任务状态（§7.5/D11）
TaskStatus = Literal["pending", "in_progress", "completed", "skipped", "cancelled"]

#: 任务来源（§7.5/D13）
TaskSource = Literal["plan", "recommendation", "manual"]

#: 个性化状态（§7.4/D14）
PersonalizationStatus = Literal["personalized", "degraded", "not_requested"]

#: 任务审计事件类型（§7.6）
TaskEventType = Literal[
    "created",
    "started",
    "completion_suggested",
    "completed",
    "reopened",
    "rescheduled",
    "skipped",
    "cancelled",
]

#: Feed run 状态（§7.7）
FeedRunStatus = Literal["queued", "running", "succeeded", "failed", "stale"]

#: Feed item 状态（§7.8）
FeedItemStatus = Literal["active", "accepted", "dismissed", "expired"]

#: Session 状态（§7.9）
SessionStatus = Literal["active", "completed", "abandoned"]

#: Conversation thread 创建状态（§7.9/D24）
ConversationStatus = Literal["not_requested", "pending", "ready", "failed"]

#: 模型调用用途（§7.11/D15）
ModelPurpose = Literal["intake", "plan", "feed", "replan"]

#: Operation 状态（§12.7；needs_input = 等待 proposed revision 决策）
OperationStatus = Literal["queued", "running", "needs_input", "succeeded", "failed", "cancelled"]

#: 推荐原因码（§11.2）
RECOMMENDATION_REASON_CODES: frozenset[str] = frozenset(
    {
        "CONTINUE_LEARNING",
        "PREREQUISITE_GAP",
        "NEXT_GRAPH_NODE",
        "REVIEW_DUE",
        "RECENT_DIFFICULTY",
        "MISSED_TASK_RECOVERY",
        "STALE_PROFICIENCY",
    }
)

#: ISO 8601 学习日（§7.3：1=周一，7=周日）
ISO_DAY_MIN = 1
ISO_DAY_MAX = 7

#: §12.3/D11 任务状态转移矩阵：{当前状态: {操作: 下一状态}}
TASK_TRANSITIONS: dict[str, dict[str, str]] = {
    "pending": {
        "start": "in_progress",
        "complete": "completed",
        "skip": "skipped",
        "reschedule": "pending",
    },
    "in_progress": {"complete": "completed", "skip": "skipped"},
    "completed": {"reopen": "pending"},
    "skipped": {"reopen": "pending"},
    "cancelled": {},
}

#: 每日最多任务数（§10.5）
MAX_TASKS_PER_DAY = 4

#: 每周时间缓冲比例（§10.6）
WEEKLY_BUFFER_RATIO = 0.10

#: 复习间隔（天，§10.7）
REVIEW_INTERVAL_DAYS: tuple[int, ...] = (1, 3, 7)

#: 第一版每日最多展示的额外自适应推荐数（§11.2）
MAX_DAILY_RECOMMENDATIONS = 2

#: 近 7 天窗口（§13.3）
RECENT_DAYS_WINDOW = 7
