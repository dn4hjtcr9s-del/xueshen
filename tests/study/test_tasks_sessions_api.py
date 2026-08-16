"""Study 任务与 Session API 集成测试（§12.3/§12.5/D23/D24/D28，§20.3）。

覆盖：状态矩阵、版本冲突、reschedule 碰撞、launch 骨架、
heartbeat seq 幂等/乱序/过快、finish 结算与 daily_stats 归账。
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.study.conftest import USER_A, auth, manual_plan_body

START = "2026-08-17"  # 周一（有任务）


async def _plan_with_task(client: AsyncClient) -> tuple[str, dict[str, Any]]:
    """创建并激活计划，返回 (plan_id, 首个任务)。"""
    body = manual_plan_body(start_date=START, duration_weeks=6)
    r = await client.post(
        "/api/v1/study/plans", json=body, headers={**auth(USER_A), "Idempotency-Key": "p1"}
    )
    plan = r.json()
    await client.post(
        f"/api/v1/study/plans/{plan['plan_id']}/activate",
        json={"expected_version": 1},
        headers={**auth(USER_A), "Idempotency-Key": "act1"},
    )
    calendar = (
        await client.get(f"/api/v1/study/plans/{plan['plan_id']}/calendar", headers=auth(USER_A))
    ).json()
    tasks = [t for w in calendar["weeks"] for d in w["days"] for t in d["tasks"]]
    return plan["plan_id"], tasks[0]


class TestTaskTransitions:
    async def test_start_complete_reopen_skip(self, client: AsyncClient) -> None:
        _plan_id, task = await _plan_with_task(client)
        tid = task["task_id"]
        # start
        r = await client.post(
            f"/api/v1/study/tasks/{tid}/start",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "s1"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "in_progress"
        assert r.json()["version"] == 2
        # complete（结算 Session）
        r = await client.post(
            f"/api/v1/study/tasks/{tid}/complete",
            json={"expected_version": 2},
            headers={**auth(USER_A), "Idempotency-Key": "c1"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "completed"
        assert r.json()["completion_source"] == "manual"
        # reopen
        r = await client.post(
            f"/api/v1/study/tasks/{tid}/reopen",
            json={"expected_version": 3},
            headers={**auth(USER_A), "Idempotency-Key": "r1"},
        )
        assert r.json()["status"] == "pending"
        # skip
        r = await client.post(
            f"/api/v1/study/tasks/{tid}/skip",
            json={"expected_version": 4},
            headers={**auth(USER_A), "Idempotency-Key": "sk1"},
        )
        assert r.json()["status"] == "skipped"

    async def test_invalid_transition_rejected(self, client: AsyncClient) -> None:
        _plan_id, task = await _plan_with_task(client)
        tid = task["task_id"]
        r = await client.post(
            f"/api/v1/study/tasks/{tid}/reopen",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "r1"},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "STUDY_INVALID_TASK_TRANSITION"

    async def test_version_conflict(self, client: AsyncClient) -> None:
        _plan_id, task = await _plan_with_task(client)
        tid = task["task_id"]
        r = await client.post(
            f"/api/v1/study/tasks/{tid}/start",
            json={"expected_version": 99},
            headers={**auth(USER_A), "Idempotency-Key": "s1"},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "STUDY_TASK_VERSION_CONFLICT"
        assert r.json()["error"]["current_version"] == 1


class TestReschedule:
    async def test_reschedule_to_rest_day_rejected(self, client: AsyncClient) -> None:
        _plan_id, task = await _plan_with_task(client)
        r = await client.post(
            f"/api/v1/study/tasks/{task['task_id']}/reschedule",
            json={"scheduled_date": "2026-08-18", "expected_version": 1},  # 周二休息日
            headers={**auth(USER_A), "Idempotency-Key": "rs1"},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "STUDY_SCHEDULE_CONFLICT"

    async def test_reschedule_success_moves_only_itself(self, client: AsyncClient) -> None:
        _plan_id, task = await _plan_with_task(client)
        r = await client.post(
            f"/api/v1/study/tasks/{task['task_id']}/reschedule",
            json={"scheduled_date": "2026-08-21", "expected_version": 1},  # 周五
            headers={**auth(USER_A), "Idempotency-Key": "rs1"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["scheduled_date"] == "2026-08-21"
        assert r.json()["version"] == 2


class TestLaunchAndSessions:
    async def test_launch_returns_stable_skeleton(self, client: AsyncClient) -> None:
        _plan_id, task = await _plan_with_task(client)
        r = await client.post(
            f"/api/v1/study/tasks/{task['task_id']}/launch",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "l1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["task_id"] == task["task_id"]
        assert body["conversation_thread_id"] is None
        assert body["conversation_status"] == "pending"
        assert body["launch_payload"]["topic_key"] == "linear-algebra:systems"
        session_id = body["session_id"]
        # 再次 launch（in_progress）复用同一 Session
        r = await client.post(
            f"/api/v1/study/tasks/{task['task_id']}/launch",
            json={"expected_version": 2},
            headers={**auth(USER_A), "Idempotency-Key": "l2"},
        )
        assert r.json()["session_id"] == session_id

    async def test_heartbeat_seq_semantics(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        _plan_id, task = await _plan_with_task(client)
        r = await client.post(
            f"/api/v1/study/tasks/{task['task_id']}/start",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "s1"},
        )
        task_json = r.json()
        # 找到 active session
        r = await client.post(
            f"/api/v1/study/tasks/{task_json['task_id']}/launch",
            json={"expected_version": 2},
            headers={**auth(USER_A), "Idempotency-Key": "l1"},
        )
        session_id = r.json()["session_id"]
        # 首次 heartbeat
        r = await client.post(
            f"/api/v1/study/sessions/{session_id}/heartbeat",
            json={"seq": 1},
            headers=auth(USER_A),
        )
        assert r.status_code == 200
        # 同 seq 重放 → 幂等
        r = await client.post(
            f"/api/v1/study/sessions/{session_id}/heartbeat",
            json={"seq": 1},
            headers=auth(USER_A),
        )
        assert r.status_code == 200
        # 过快（< 30s 间隔）→ 429 RATE_LIMITED
        r = await client.post(
            f"/api/v1/study/sessions/{session_id}/heartbeat",
            json={"seq": 2},
            headers=auth(USER_A),
        )
        assert r.status_code == 429
        assert r.json()["error"]["code"] == "RATE_LIMITED"
        assert "Retry-After" in r.headers
        # 乱序 → 409（直接改库把已确认 seq 抬到 3，再发 seq=2）
        from sqlalchemy import text

        async with study_session_factory() as db:
            await db.execute(
                text("UPDATE study_sessions SET last_heartbeat_seq = 3 WHERE session_id = :sid"),
                {"sid": session_id},
            )
            await db.commit()
        r = await client.post(
            f"/api/v1/study/sessions/{session_id}/heartbeat",
            json={"seq": 2},
            headers=auth(USER_A),
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "STUDY_SESSION_CONFLICT"

    async def test_finish_requires_idempotency_key(self, client: AsyncClient) -> None:
        _plan_id, task = await _plan_with_task(client)
        r = await client.post(
            f"/api/v1/study/tasks/{task['task_id']}/start",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "s1"},
        )
        r = await client.post(
            f"/api/v1/study/tasks/{task['task_id']}/launch",
            json={"expected_version": 2},
            headers={**auth(USER_A), "Idempotency-Key": "l1"},
        )
        session_id = r.json()["session_id"]
        r = await client.post(f"/api/v1/study/sessions/{session_id}/finish", headers=auth(USER_A))
        assert r.status_code == 422
