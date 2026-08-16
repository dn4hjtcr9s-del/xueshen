"""Study 域公开错误模型与错误码（docs/study-plan-push-implementation-plan.md v1.2，§17）。

与 Conversation/Community 同模式：信封复用共享 PublicError
（backend/memory/contracts/errors.py），HTTP 输出由 app.py 的
exception handler 统一转换。版本冲突（409）时错误对象可携带
current_version 供客户端重试（§15.3，与 Conversation 同语义）。
"""

from __future__ import annotations

#: Study 域错误码全集（§17 冻结）
STUDY_ERROR_CODES: frozenset[str] = frozenset(
    {
        "STUDY_PLAN_NOT_FOUND",
        "STUDY_INTAKE_EXPIRED",
        "STUDY_INTAKE_LIMIT_EXCEEDED",
        "STUDY_INTAKE_NOT_FOUND",
        "ACTIVE_STUDY_PLAN_EXISTS",
        "STUDY_PLAN_VERSION_CONFLICT",
        "STUDY_TASK_VERSION_CONFLICT",
        "STUDY_PLAN_INPUT_INCOMPLETE",
        "STUDY_PLAN_INFEASIBLE",
        "STUDY_PLAN_GENERATION_FAILED",
        "STUDY_TASK_NOT_FOUND",
        "STUDY_INVALID_TASK_TRANSITION",
        "STUDY_SCHEDULE_CONFLICT",
        "STUDY_IDEMPOTENCY_CONFLICT",
        "STUDY_FEED_ITEM_NOT_FOUND",
        "STUDY_RECOMMENDATION_EXPIRED",
        "STUDY_NO_ACTIVE_PLAN",
        "STUDY_REVISION_NOT_FOUND",
        "STUDY_INVALID_REVISION_TRANSITION",
        "STUDY_SESSION_CONFLICT",
        "STUDY_OPERATION_NOT_FOUND",
        # 实现期补充（§12.2 生命周期端点需要区分计划状态转移错误；
        # 与 D21 的 revision 转移错误分离，避免语义混淆）
        "STUDY_INVALID_PLAN_TRANSITION",
    }
)


class StudyError(Exception):
    """Study 业务错误基类。http_status 与公开错误码一一对应（§17）。"""

    code: str = "INTERNAL_ERROR"
    http_status: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str,
        *,
        field: str | None = None,
        current_version: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.field = field
        self.current_version = current_version


class StudyPlanNotFoundError(StudyError):
    """计划不存在或不属于当前用户（§17：404，不泄露对象状态）。"""

    code = "STUDY_PLAN_NOT_FOUND"
    http_status = 404


class StudyIntakeExpiredError(StudyError):
    """intake 已超过有效期（§12.1：409，客户端必须新建 intake）。"""

    code = "STUDY_INTAKE_EXPIRED"
    http_status = 409


class StudyIntakeNotFoundError(StudyError):
    """intake 不存在或不属于当前用户（§12.1 实现期补充：404）。"""

    code = "STUDY_INTAKE_NOT_FOUND"
    http_status = 404


class StudyIntakeLimitExceededError(StudyError):
    """intake 达到 8 轮/2,000 字符/24 小时上限（§12.1：409）。"""

    code = "STUDY_INTAKE_LIMIT_EXCEEDED"
    http_status = 409


class ActiveStudyPlanExistsError(StudyError):
    """activate/resume 撞到其他 active 计划（D25：409，不自动修改旧计划）。"""

    code = "ACTIVE_STUDY_PLAN_EXISTS"
    http_status = 409


class StudyPlanVersionConflictError(StudyError):
    """计划 expected_version 不匹配（§15.3：409，携带 current_version）。"""

    code = "STUDY_PLAN_VERSION_CONFLICT"
    http_status = 409


class StudyTaskVersionConflictError(StudyError):
    """任务 expected_version 不匹配（§15.3：409，携带 current_version）。"""

    code = "STUDY_TASK_VERSION_CONFLICT"
    http_status = 409


class StudyPlanInputIncompleteError(StudyError):
    """PlanIntent 信息不足（§8：422，禁止模型自行补造）。"""

    code = "STUDY_PLAN_INPUT_INCOMPLETE"
    http_status = 422


class StudyPlanInfeasibleError(StudyError):
    """时间预算无法在截止日期前完成目标（§10.12：409）。"""

    code = "STUDY_PLAN_INFEASIBLE"
    http_status = 409


class StudyPlanGenerationFailedError(StudyError):
    """计划生成 operation 失败（§16：503，重试安全）。"""

    code = "STUDY_PLAN_GENERATION_FAILED"
    http_status = 503
    retryable = True


class StudyTaskNotFoundError(StudyError):
    """任务不存在或不属于当前用户（§17：404）。"""

    code = "STUDY_TASK_NOT_FOUND"
    http_status = 404


class StudyInvalidTaskTransitionError(StudyError):
    """任务状态转移不在冻结矩阵内（§12.3/D11：409）。"""

    code = "STUDY_INVALID_TASK_TRANSITION"
    http_status = 409


class StudyScheduleConflictError(StudyError):
    """reschedule 目标日负载超限（§12.3：409，adjustment_required=true）。"""

    code = "STUDY_SCHEDULE_CONFLICT"
    http_status = 409

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message, field=field)
        self.adjustment_required = True


class StudyIdempotencyConflictError(StudyError):
    """同幂等键不同规范化请求体（D16/§15.1：409）。"""

    code = "STUDY_IDEMPOTENCY_CONFLICT"
    http_status = 409


class StudyFeedItemNotFoundError(StudyError):
    """feed item 不存在或不属于当前用户（§17：404）。"""

    code = "STUDY_FEED_ITEM_NOT_FOUND"
    http_status = 404


class StudyRecommendationExpiredError(StudyError):
    """推荐已过期或已处理（§11.2：409）。"""

    code = "STUDY_RECOMMENDATION_EXPIRED"
    http_status = 409


class StudyNoActivePlanError(StudyError):
    """ensure-today 时无 active plan（D22：409，零副作用）。"""

    code = "STUDY_NO_ACTIVE_PLAN"
    http_status = 409


class StudyRevisionNotFoundError(StudyError):
    """revision 不存在或不属于当前用户计划（§17：404）。"""

    code = "STUDY_REVISION_NOT_FOUND"
    http_status = 404


class StudyInvalidRevisionTransitionError(StudyError):
    """proposed revision 的 accept/reject 状态转移非法（§12.2/D21：409）。"""

    code = "STUDY_INVALID_REVISION_TRANSITION"
    http_status = 409


class StudySessionConflictError(StudyError):
    """heartbeat seq 乱序或 Session 状态冲突（§12.5/D28：409）。"""

    code = "STUDY_SESSION_CONFLICT"
    http_status = 409


class StudyOperationNotFoundError(StudyError):
    """Study operation 不存在或不属于当前用户（§12.7：404）。"""

    code = "STUDY_OPERATION_NOT_FOUND"
    http_status = 404


class StudyInvalidPlanTransitionError(StudyError):
    """计划生命周期状态转移非法（§12.2 实现期补充：activate/pause/resume/archive）。"""

    code = "STUDY_INVALID_PLAN_TRANSITION"
    http_status = 409


class StudyRateLimitedError(StudyError):
    """速率限制（§17：429，含过快 heartbeat，错误码 RATE_LIMITED）。"""

    code = "RATE_LIMITED"
    http_status = 429
    retryable = True

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after
