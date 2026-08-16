"""Study Replan 阈值纯函数单元测试（§9.4/D18，§20.1）。"""

from __future__ import annotations

from datetime import date

from backend.study.services.replan import classify_adjustment, daily_minutes_from_tasks

MON = date(2026, 8, 17)


def _classify(
    *,
    delta: int = 0,
    removed_ratio: float = 0.0,
    target_shift_days: int = 0,
    scope: bool = False,
    chapters: bool = False,
) -> tuple[bool, bool]:
    base = {MON: 40}
    new = {MON: 40 + delta} if delta else base
    return classify_adjustment(
        base_daily_minutes=base,
        new_daily_minutes=new,
        session_min_minutes=15,
        removed_incomplete_ratio=removed_ratio,
        base_target_date=date(2026, 9, 28),
        new_target_date=(
            date(2026, 9, 28) + __import__("datetime").timedelta(days=target_shift_days)
        ),
        scope_changed=scope,
        core_chapters_changed=chapters,
    )[:2]


class TestClassifyAdjustment:
    def test_small_increase_not_major(self) -> None:
        assert _classify(delta=10) == (False, False)

    def test_increase_threshold_30pct_and_15min(self) -> None:
        # 40 → 52 = +12min：不触发；40 → 55 = +15min 且 37.5% → major
        assert _classify(delta=12) == (False, False)
        assert _classify(delta=15) == (True, False)

    def test_removed_incomplete_20pct(self) -> None:
        assert _classify(removed_ratio=0.19) == (False, False)
        assert _classify(removed_ratio=0.20) == (True, False)

    def test_target_date_any_change_major(self) -> None:
        major, high = _classify(target_shift_days=3)
        assert major is True and high is False

    def test_target_date_7_days_high_impact(self) -> None:
        major, high = _classify(target_shift_days=7)
        assert major is True and high is True

    def test_scope_and_chapters_major(self) -> None:
        assert _classify(scope=True)[0] is True
        assert _classify(chapters=True)[0] is True


class TestDailyMinutes:
    def test_ignores_cancelled(self) -> None:
        tasks = [
            {"scheduled_date": MON, "status": "pending", "estimated_minutes": 40},
            {"scheduled_date": MON, "status": "cancelled", "estimated_minutes": 20},
        ]
        assert daily_minutes_from_tasks(tasks) == {MON: 40}
