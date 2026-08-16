"""Study 进度/状态机/计时纯函数单元测试（§12.3/§13.1/§13.2，§20.1）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from backend.study.contracts.errors import StudyInvalidTaskTransitionError
from backend.study.services.progress import dual_progress, percent, task_progress
from backend.study.services.session_timing import added_active_seconds, heartbeat_decision
from backend.study.services.task_state import apply_transition, launch_transition, lifecycle_cancel

NOW = datetime(2026, 8, 16, 10, 0, 0)


class TestProgress:
    def test_percent_round_half_up(self) -> None:
        assert percent(1, 3) == 33
        assert percent(2, 3) == 67
        assert percent(1, 8) == 13  # 12.5 → 13
        assert percent(5, 8) == 63  # 62.5 → 63

    def test_zero_denominator_returns_zero(self) -> None:
        assert percent(0, 0) == 0
        assert task_progress(0, 0) == 0

    def test_clamped_0_100(self) -> None:
        assert percent(10, 1) == 100
        assert percent(-1, 5) == 0

    def test_dual_progress(self) -> None:
        assert dual_progress(2, 5, 30, 90) == (40, 33)


class TestTaskStateMachine:
    @pytest.mark.parametrize(
        ("current", "action", "expected"),
        [
            ("pending", "start", "in_progress"),
            ("pending", "complete", "completed"),
            ("pending", "skip", "skipped"),
            ("pending", "reschedule", "pending"),
            ("in_progress", "complete", "completed"),
            ("in_progress", "skip", "skipped"),
            ("completed", "reopen", "pending"),
            ("skipped", "reopen", "pending"),
        ],
    )
    def test_valid_transitions(self, current: str, action: str, expected: str) -> None:
        assert apply_transition(current, action) == expected

    @pytest.mark.parametrize(
        ("current", "action"),
        [
            ("completed", "complete"),
            ("cancelled", "start"),
            ("cancelled", "complete"),
            ("cancelled", "reopen"),
            ("in_progress", "start"),
            ("completed", "skip"),
        ],
    )
    def test_invalid_transitions_rejected(self, current: str, action: str) -> None:
        with pytest.raises(StudyInvalidTaskTransitionError):
            apply_transition(current, action)

    def test_launch_semantics(self) -> None:
        assert launch_transition("pending") == "in_progress"
        assert launch_transition("in_progress") == "in_progress"
        with pytest.raises(StudyInvalidTaskTransitionError):
            launch_transition("completed")

    def test_lifecycle_cancel(self) -> None:
        assert lifecycle_cancel("pending") == "cancelled"
        assert lifecycle_cancel("in_progress") == "cancelled"
        assert lifecycle_cancel("skipped") == "cancelled"
        assert lifecycle_cancel("completed") is None
        assert lifecycle_cancel("cancelled") is None


class TestSessionTiming:
    def test_added_seconds_rules(self) -> None:
        assert (
            added_active_seconds(gap_seconds=10, min_interval_seconds=30, idle_timeout_seconds=120)
            == 0
        )
        assert (
            added_active_seconds(gap_seconds=60, min_interval_seconds=30, idle_timeout_seconds=120)
            == 60
        )
        assert (
            added_active_seconds(gap_seconds=300, min_interval_seconds=30, idle_timeout_seconds=120)
            == 120
        )  # 空闲上限截断

    def test_heartbeat_replay_and_conflict(self) -> None:
        decision, added = heartbeat_decision(
            seq=7,
            last_seq=7,
            now=NOW,
            last_heartbeat_at=NOW - timedelta(seconds=60),
            min_interval_seconds=30,
            idle_timeout_seconds=120,
        )
        assert decision == "replay" and added == 0
        decision, added = heartbeat_decision(
            seq=6,
            last_seq=7,
            now=NOW,
            last_heartbeat_at=NOW - timedelta(seconds=60),
            min_interval_seconds=30,
            idle_timeout_seconds=120,
        )
        assert decision == "conflict" and added == 0

    def test_heartbeat_too_fast(self) -> None:
        decision, added = heartbeat_decision(
            seq=8,
            last_seq=7,
            now=NOW,
            last_heartbeat_at=NOW - timedelta(seconds=10),
            min_interval_seconds=30,
            idle_timeout_seconds=120,
        )
        assert decision == "too_fast" and added == 0

    def test_heartbeat_accepted_first_and_normal(self) -> None:
        decision, added = heartbeat_decision(
            seq=1,
            last_seq=0,
            now=NOW,
            last_heartbeat_at=None,
            min_interval_seconds=30,
            idle_timeout_seconds=120,
        )
        assert decision == "accepted" and added == 0
        decision, added = heartbeat_decision(
            seq=2,
            last_seq=1,
            now=NOW,
            last_heartbeat_at=NOW - timedelta(seconds=60),
            min_interval_seconds=30,
            idle_timeout_seconds=120,
        )
        assert decision == "accepted" and added == 60
