"""Study 确定性排期引擎（方案 §10/D12，纯函数，无 I/O）。

- 模型/用户的原始预计分钟不能直接落库：先按 5 分钟粒度归一化（ROUND_HALF_UP），
  再限制在 [session_min, session_max]；超过上限拆分并重新平衡尾段；
- 手动蓝图按顺序铺到非休息日，每天最多 MAX_TASKS_PER_DAY 个任务、
  单日负载不超过当天 available_minutes；放不下时顺延到下一个非休息日；
- 截止日期内放不下 → 直接抛 StudyPlanInfeasibleError（§10.12，不静默超额）。

所有函数必须是纯函数并覆盖边界测试（§20.1），禁止引入时间/时区的隐式依赖：
调用方传入按计划时区计算好的日期序列。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from backend.study.contracts.domain import ISO_DAY_MAX, ISO_DAY_MIN, MAX_TASKS_PER_DAY
from backend.study.contracts.errors import StudyPlanInfeasibleError

#: §10.3：5 分钟粒度
MINUTE_GRANULARITY = 5


@dataclass(frozen=True)
class ScheduledTaskDraft:
    """排期引擎输出的一条正式任务（未落库，供事务写入）。"""

    title: str
    task_type: str
    estimated_minutes: int
    model_estimated_minutes: int | None
    estimation_basis: str
    topic_key: str | None
    description: str
    scheduled_date: date
    order_index: int


@dataclass(frozen=True)
class DayPlan:
    """某一自然日的排期视图（§10 规则输入）。"""

    local_date: date
    day_of_week: int  # ISO 8601
    available_minutes: int
    is_rest_day: bool


@dataclass
class DayBucket:
    """排期过程中的日负载累加器。"""

    day: DayPlan
    used_minutes: int = 0
    task_count: int = 0
    tasks: list[ScheduledTaskDraft] = field(default_factory=list)

    def remaining_minutes(self) -> int:
        return max(0, self.day.available_minutes - self.used_minutes)

    def can_accept(self, minutes: int) -> bool:
        if self.day.is_rest_day:
            return False
        if self.task_count >= MAX_TASKS_PER_DAY:
            return False
        return self.used_minutes + minutes <= self.day.available_minutes


def normalize_minutes(
    raw_minutes: int, session_min: int, session_max: int
) -> tuple[list[int], str]:
    """§10.3/D12：归一化 + clamp + 拆分，返回 (片段列表, estimation_basis)。

    - 5 分钟粒度归一化（ROUND_HALF_UP）；
    - 低于 session_min → clamp 到 session_min（basis=clamp）；
    - 高于 session_max → 拆分为多个 ≤ session_max 的片段并平衡尾段
      （尾段不足 session_min 时并入上一片段，basis=split）；
    - 恰好落入区间 → 原值（basis=original，除非发生过归一化进位）。
    """
    if raw_minutes <= 0:
        raise ValueError("estimated_minutes 必须大于 0")
    if session_min <= 0 or session_max < session_min:
        raise ValueError("session 区间非法")

    granularity = MINUTE_GRANULARITY
    normalized = round(raw_minutes / granularity) * granularity
    rounded = normalized != raw_minutes

    if normalized < session_min:
        return [session_min], "clamp" if (rounded or raw_minutes < session_min) else "original"
    if normalized <= session_max:
        return [normalized], "original" if not rounded else "clamp"

    # 拆分：整段 session_max + 尾段；尾段不足 session_min 时并入上一片段
    full, tail = divmod(normalized, session_max)
    pieces = [session_max] * full
    if tail > 0:
        if tail < session_min and pieces:
            pieces[-1] += tail
        else:
            pieces.append(max(tail, session_min))
    return pieces, "split"


def plan_days(start: date, end: date, availability: dict[int, DayPlan]) -> list[DayPlan]:
    """展开 [start, end] 的自然日序列（§13.3：含休息日，保持日历完整）。

    availability 只提供"周几 → 分钟/休息日"模板，local_date 一律由本函数
    按游标真实日期生成（模板的 local_date 字段被忽略）。
    """
    days: list[DayPlan] = []
    cursor = start
    while cursor <= end:
        dow = cursor.isoweekday()
        slot = availability.get(dow)
        if slot is None:
            days.append(
                DayPlan(local_date=cursor, day_of_week=dow, available_minutes=0, is_rest_day=True)
            )
        else:
            days.append(
                DayPlan(
                    local_date=cursor,
                    day_of_week=dow,
                    available_minutes=slot.available_minutes,
                    is_rest_day=slot.is_rest_day,
                )
            )
        cursor += timedelta(days=1)
    return days


def schedule_manual_blueprint(
    *,
    days: list[DayPlan],
    session_min: int,
    session_max: int,
    blueprints: list[tuple[str, str, int, str | None, str]],
) -> list[ScheduledTaskDraft]:
    """把用户确认的任务蓝图确定性铺进日期序列（§10/D8）。

    blueprints 元素 = (title, task_type, estimated_minutes, topic_key, description)。
    规则：跳过休息日；每天最多 4 个任务；单日负载不超 available_minutes；
    放不下 → 顺延；所有日期耗尽 → StudyPlanInfeasibleError。
    """
    if not blueprints:
        raise ValueError("task_blueprint 不能为空")
    # 可行性预检（§10.12）：总需求不超过总供给
    total_needed = sum(bp[2] for bp in blueprints)
    total_available = sum(d.available_minutes for d in days if not d.is_rest_day)
    if total_needed > total_available:
        raise StudyPlanInfeasibleError(
            "当前时间预算无法在截止日期前完成目标：总需求"
            f" {total_needed} 分钟超过可用 {total_available} 分钟"
        )

    buckets = [DayBucket(day=d) for d in days if not d.is_rest_day]
    bucket_index = 0
    result: list[ScheduledTaskDraft] = []

    for title, task_type, raw_minutes, topic_key, description in blueprints:
        pieces, basis = normalize_minutes(raw_minutes, session_min, session_max)
        piece_count = len(pieces)
        for piece_no, minutes in enumerate(pieces, start=1):
            placed = False
            attempts = 0
            while attempts < len(buckets):
                bucket = buckets[bucket_index % len(buckets)]
                bucket_index += 1
                attempts += 1
                if bucket.can_accept(minutes):
                    bucket.used_minutes += minutes
                    bucket.task_count += 1
                    suffix = f"（拆分 {piece_no}/{piece_count}）" if piece_count > 1 else ""
                    result.append(
                        ScheduledTaskDraft(
                            title=title + suffix,
                            task_type=task_type,
                            estimated_minutes=minutes,
                            model_estimated_minutes=raw_minutes,
                            estimation_basis=basis,
                            topic_key=topic_key,
                            description=description,
                            scheduled_date=bucket.day.local_date,
                            order_index=bucket.task_count,
                        )
                    )
                    placed = True
                    break
            if not placed:
                raise StudyPlanInfeasibleError(f"任务「{title}」无法在截止日期前排入任何学习日")
    return result


def validate_day_of_week(dow: int) -> bool:
    """ISO 8601 周几合法性（§7.3）。"""
    return ISO_DAY_MIN <= dow <= ISO_DAY_MAX
