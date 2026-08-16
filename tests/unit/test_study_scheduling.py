"""Study 确定性排期引擎单元测试（§10/§20.1/D12）。"""

from __future__ import annotations

from datetime import date

import pytest

from backend.study.contracts.errors import StudyPlanInfeasibleError
from backend.study.services.scheduling import (
    DayPlan,
    normalize_minutes,
    plan_days,
    schedule_manual_blueprint,
)


def _day(d: date, minutes: int, rest: bool = False) -> DayPlan:
    return DayPlan(
        local_date=d, day_of_week=d.isoweekday(), available_minutes=minutes, is_rest_day=rest
    )


class TestNormalizeMinutes:
    def test_below_min_clamps(self) -> None:
        assert normalize_minutes(8, 15, 60) == ([15], "clamp")

    def test_within_range_original(self) -> None:
        assert normalize_minutes(40, 15, 60) == ([40], "original")

    def test_five_minute_granularity_rounds(self) -> None:
        # 43 → 45（5 分钟粒度 ROUND_HALF_UP）
        assert normalize_minutes(43, 15, 60) == ([45], "clamp")
        assert normalize_minutes(42, 15, 60) == ([40], "clamp")

    def test_over_max_splits_even(self) -> None:
        assert normalize_minutes(120, 15, 60) == ([60, 60], "split")

    def test_over_max_balances_tail(self) -> None:
        # 130 → 60 + 60 + 尾段 10（不足 session_min）→ 尾段并入上一片段 [60, 70]
        pieces, basis = normalize_minutes(130, 15, 60)
        assert basis == "split"
        assert sum(pieces) == 130
        assert pieces == [60, 70]

    def test_over_max_tail_above_min_kept(self) -> None:
        # 100 → 60 + 40（尾段 ≥ 15 保留）
        assert normalize_minutes(100, 15, 60) == ([60, 40], "split")


class TestPlanDays:
    def test_week_expansion_includes_rest_days(self) -> None:
        start = date(2026, 8, 17)  # 周一
        availability = {
            1: _day(start, 40),
            3: _day(start + __import__("datetime").timedelta(days=2), 40),
        }
        days = plan_days(start, start + __import__("datetime").timedelta(days=6), availability)
        assert len(days) == 7
        # 周二没有模板 → 默认休息日
        assert days[1].is_rest_day is True
        assert days[0].is_rest_day is False
        assert days[0].day_of_week == 1


class TestScheduleManualBlueprint:
    def test_places_on_non_rest_days_in_order(self) -> None:
        start = date(2026, 8, 17)  # 周一
        days = [
            _day(start, 40),
            _day(start + __import__("datetime").timedelta(days=1), 0, rest=True),
            _day(start + __import__("datetime").timedelta(days=2), 40),
        ]
        blueprints = [
            ("任务1", "learn", 40, None, ""),
            ("任务2", "practice", 30, None, ""),
        ]
        result = schedule_manual_blueprint(
            days=days, session_min=15, session_max=60, blueprints=blueprints
        )
        assert [t.scheduled_date for t in result] == [
            start,
            start + __import__("datetime").timedelta(days=2),
        ]
        assert [t.order_index for t in result] == [1, 1]

    def test_day_budget_enforced(self) -> None:
        start = date(2026, 8, 17)
        # §10.6 周缓冲：周容量 = (50+50)×90% = 90 ≥ 80 可行
        days = [_day(start, 50), _day(start + __import__("datetime").timedelta(days=2), 50)]
        blueprints = [("任务1", "learn", 40, None, ""), ("任务2", "learn", 40, None, "")]
        result = schedule_manual_blueprint(
            days=days, session_min=15, session_max=60, blueprints=blueprints
        )
        # 任务1 周一（余 10 放不下任务2），任务2 顺延到周三
        assert [t.scheduled_date for t in result] == [
            start,
            start + __import__("datetime").timedelta(days=2),
        ]

    def test_max_four_tasks_per_day(self) -> None:
        start = date(2026, 8, 17)
        days = [
            _day(start, 400),
            _day(start + __import__("datetime").timedelta(days=2), 400),
        ]
        blueprints = [(f"任务{i}", "learn", 20, None, "") for i in range(6)]
        result = schedule_manual_blueprint(
            days=days, session_min=15, session_max=60, blueprints=blueprints
        )
        per_day: dict[date, int] = {}
        for t in result:
            per_day[t.scheduled_date] = per_day.get(t.scheduled_date, 0) + 1
        # 每天最多 4 个任务：6 个任务分摊到两天
        assert max(per_day.values()) <= 4
        assert len(per_day) == 2

    def test_infeasible_raises(self) -> None:
        start = date(2026, 8, 17)
        days = [_day(start, 40)]
        with pytest.raises(StudyPlanInfeasibleError):
            schedule_manual_blueprint(
                days=days,
                session_min=15,
                session_max=60,
                blueprints=[("任务1", "learn", 50, None, "")],
            )

    def test_split_tasks_get_suffix_and_basis(self) -> None:
        start = date(2026, 8, 17)
        days = [_day(start, 400)]
        result = schedule_manual_blueprint(
            days=days,
            session_min=15,
            session_max=60,
            blueprints=[("大任务", "learn", 130, None, "")],
        )
        assert len(result) == 2
        assert result[0].estimation_basis == "split"
        assert "拆分" in result[0].title
        assert result[0].model_estimated_minutes == 130


class TestWeeklyBufferAndReviewIntervals:
    """§10.6 周缓冲 + §10.7 复习间隔（评审必改 #9/#10）。"""

    def test_weekly_buffer_caps_week_total(self) -> None:
        start = date(2026, 8, 17)  # 周一
        # 一周三天 × 40 分钟 = 120；周容量 = 108（90%）。4 × 30 = 120 > 108 → 不可行
        days = [
            _day(start, 40),
            _day(start + __import__("datetime").timedelta(days=2), 40),
            _day(start + __import__("datetime").timedelta(days=4), 40),
        ]
        with pytest.raises(StudyPlanInfeasibleError):
            schedule_manual_blueprint(
                days=days,
                session_min=15,
                session_max=60,
                blueprints=[(f"任务{i}", "learn", 30, None, "") for i in range(4)],
            )

    def test_weekly_buffer_allows_within_capacity(self) -> None:
        start = date(2026, 8, 17)
        days = [
            _day(start, 40),
            _day(start + __import__("datetime").timedelta(days=2), 40),
            _day(start + __import__("datetime").timedelta(days=4), 40),
        ]
        # 3 × 30 = 90 ≤ 108 → 可行
        result = schedule_manual_blueprint(
            days=days,
            session_min=15,
            session_max=60,
            blueprints=[(f"任务{i}", "learn", 30, None, "") for i in range(3)],
        )
        assert len(result) == 3

    def test_review_prefers_1_3_7_intervals(self) -> None:
        start = date(2026, 8, 17)  # 周一
        days = []
        for i in range(14):
            d = start + __import__("datetime").timedelta(days=i)
            if d.isoweekday() in (1, 3, 5):
                days.append(_day(d, 120))
        result = schedule_manual_blueprint(
            days=days,
            session_min=15,
            session_max=60,
            blueprints=[
                ("学向量", "learn", 30, "va", ""),
                ("复习向量", "review", 30, "va", ""),
            ],
        )
        learn = next(t for t in result if t.task_type == "learn")
        review = next(t for t in result if t.task_type == "review")
        gap = (review.scheduled_date - learn.scheduled_date).days
        # 学任务周一（8/17）→ 复习优先 +1（周二无课）→ +3（周四无课）
        # → +7（下周一 8/24 有课）→ 严格命中 1/3/7 间隔链
        assert gap == 7
        assert review.scheduled_date > learn.scheduled_date
