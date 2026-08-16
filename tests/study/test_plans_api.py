"""Study 计划 API 集成测试（§12.2/D5/D8/D21/D25/D26，§20.3）。

覆盖：manual 直录端到端、calendar 形状、active 唯一约束、版本冲突、
幂等重放/冲突、revision 决策、生命周期与用户隔离。
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.study.conftest import USER_A, USER_B, auth, manual_plan_body

START = "2026-08-17"  # 周一


async def _create_plan(client: AsyncClient, **overrides: Any) -> dict[str, Any]:
    key = overrides.pop("_key", None)
    body = manual_plan_body(start_date=START, duration_weeks=6)
    body.update(overrides)
    r = await client.post(
        "/api/v1/study/plans",
        json=body,
        headers={**auth(USER_A), "Idempotency-Key": key or f"k-{abs(hash(str(body))) % 10**8}"},
    )
    assert r.status_code == 201, r.text
    return r.json()


class TestManualCreation:
    async def test_create_manual_draft_plan(self, client: AsyncClient) -> None:
        plan = await _create_plan(client)
        assert plan["status"] == "draft"
        assert plan["current_revision_id"] is not None
        assert plan["personalization_status"] == "not_requested"
        assert plan["weekly_minutes"] == 140

    async def test_calendar_covers_full_range_with_rest_days(self, client: AsyncClient) -> None:
        plan = await _create_plan(client)
        r = await client.get(
            f"/api/v1/study/plans/{plan['plan_id']}/calendar", headers=auth(USER_A)
        )
        assert r.status_code == 200, r.text
        calendar = r.json()
        assert calendar["timezone"] == "Asia/Shanghai"
        assert calendar["start_date"] == START
        assert calendar["target_date"] == "2026-09-28"
        # 完整范围覆盖：2026-08-17(一) 至 09-28(一)；最后一周仅 1 天
        assert len(calendar["weeks"]) == 7
        first_week = calendar["weeks"][0]
        assert first_week["week_index"] == 1
        assert len(first_week["days"]) == 7
        tuesday = first_week["days"][1]
        assert tuesday["local_date"] == "2026-08-18"
        assert tuesday["is_rest_day"] is True
        assert tuesday["tasks"] == []
        monday = first_week["days"][0]
        assert monday["is_rest_day"] is False
        assert monday["available_minutes"] == 40
        assert len(monday["tasks"]) == 1
        assert monday["tasks"][0]["title"] == "矩阵与线性方程组"

    async def test_ai_mode_returns_queued_operation(self, client: AsyncClient) -> None:
        body = manual_plan_body(start_date=START, duration_weeks=6)
        body["generation_mode"] = "ai"
        body.pop("task_blueprint")
        r = await client.post(
            "/api/v1/study/plans", json=body, headers={**auth(USER_A), "Idempotency-Key": "ai-1"}
        )
        assert r.status_code == 202, r.text
        op = r.json()["operation_id"]
        r2 = await client.get(f"/api/v1/study/operations/{op}", headers=auth(USER_A))
        assert r2.status_code == 200
        assert r2.json()["status"] == "queued"

    async def test_intent_constraints_422(self, client: AsyncClient) -> None:
        body = manual_plan_body(start_date=START, target_date="2026-08-10")
        r = await client.post(
            "/api/v1/study/plans", json=body, headers={**auth(USER_A), "Idempotency-Key": "bad-1"}
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_PAYLOAD"


class TestLifecycle:
    async def test_activate_and_active_uniqueness(self, client: AsyncClient) -> None:
        plan1 = await _create_plan(client, _key="plan-1")
        plan2 = await _create_plan(client, _key="plan-2")
        r = await client.post(
            f"/api/v1/study/plans/{plan1['plan_id']}/activate",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "act-1"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "active"
        assert r.json()["version"] == 2
        # 第二个计划激活 → D5 唯一约束 409
        r = await client.post(
            f"/api/v1/study/plans/{plan2['plan_id']}/activate",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "act-2"},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "ACTIVE_STUDY_PLAN_EXISTS"

    async def test_version_conflict_409_with_current_version(self, client: AsyncClient) -> None:
        plan = await _create_plan(client)
        await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/activate",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "act-1"},
        )
        r = await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/pause",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "pause-1"},
        )
        assert r.status_code == 409
        body = r.json()["error"]
        assert body["code"] == "STUDY_PLAN_VERSION_CONFLICT"
        assert body["current_version"] == 2

    async def test_pause_resume_archive_cancels_tasks(self, client: AsyncClient) -> None:
        plan = await _create_plan(client)
        await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/activate",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "act-1"},
        )
        r = await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/pause",
            json={"expected_version": 2},
            headers={**auth(USER_A), "Idempotency-Key": "pause-1"},
        )
        assert r.json()["status"] == "paused"
        r = await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/resume",
            json={"expected_version": 3},
            headers={**auth(USER_A), "Idempotency-Key": "resume-1"},
        )
        assert r.json()["status"] == "active"
        r = await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/archive",
            json={"expected_version": 4},
            headers={**auth(USER_A), "Idempotency-Key": "archive-1"},
        )
        assert r.json()["status"] == "archived"
        calendar = (
            await client.get(
                f"/api/v1/study/plans/{plan['plan_id']}/calendar", headers=auth(USER_A)
            )
        ).json()
        tasks = [t for w in calendar["weeks"] for d in w["days"] for t in d["tasks"]]
        assert all(t["status"] == "cancelled" for t in tasks)


class TestRevisionDecision:
    async def test_accept_initial_revision_activates(self, client: AsyncClient) -> None:
        plan = await _create_plan(client)
        revision_id = plan["current_revision_id"]
        r = await client.get(
            f"/api/v1/study/plans/{plan['plan_id']}/revisions", headers=auth(USER_A)
        )
        assert len(r.json()) == 1
        assert r.json()[0]["status"] == "proposed"
        r = await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/revisions/{revision_id}/accept",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "acc-1"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["revision"]["status"] == "active"
        assert r.json()["revision"]["decision_reason"] is None

    async def test_reject_keeps_current_state(self, client: AsyncClient) -> None:
        plan = await _create_plan(client)
        revision_id = plan["current_revision_id"]
        r = await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/revisions/{revision_id}/reject",
            json={"expected_version": 1, "reason": "暂不需要"},
            headers={**auth(USER_A), "Idempotency-Key": "rej-1"},
        )
        assert r.status_code == 200
        assert r.json()["revision"]["status"] == "rejected"
        assert r.json()["revision"]["decision_reason"] == "暂不需要"

    async def test_accept_twice_is_invalid_transition(self, client: AsyncClient) -> None:
        plan = await _create_plan(client)
        revision_id = plan["current_revision_id"]
        r = await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/revisions/{revision_id}/accept",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "acc-1"},
        )
        assert r.status_code == 200, r.text
        # 已 active 的 revision 再次 accept → 409（新幂等键，非重放）
        r = await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/revisions/{revision_id}/accept",
            json={"expected_version": 2},
            headers={**auth(USER_A), "Idempotency-Key": "acc-2"},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "STUDY_INVALID_REVISION_TRANSITION"


class TestIdempotencyAndIsolation:
    async def test_same_key_replays_first_result(self, client: AsyncClient) -> None:
        body = manual_plan_body(start_date=START, duration_weeks=6)
        headers = {**auth(USER_A), "Idempotency-Key": "same-key-1"}
        r1 = await client.post("/api/v1/study/plans", json=body, headers=headers)
        r2 = await client.post("/api/v1/study/plans", json=body, headers=headers)
        assert r1.status_code == r2.status_code == 201
        assert r1.json()["plan_id"] == r2.json()["plan_id"]

    async def test_same_key_different_payload_409(self, client: AsyncClient) -> None:
        r1 = await client.post(
            "/api/v1/study/plans",
            json=manual_plan_body(start_date=START, duration_weeks=6),
            headers={**auth(USER_A), "Idempotency-Key": "conflict-key"},
        )
        assert r1.status_code == 201
        r2 = await client.post(
            "/api/v1/study/plans",
            json=manual_plan_body(start_date=START, duration_weeks=4),
            headers={**auth(USER_A), "Idempotency-Key": "conflict-key"},
        )
        assert r2.status_code == 409
        assert r2.json()["error"]["code"] == "STUDY_IDEMPOTENCY_CONFLICT"

    async def test_missing_idempotency_key_422(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/v1/study/plans",
            json=manual_plan_body(start_date=START, duration_weeks=6),
            headers=auth(USER_A),
        )
        assert r.status_code == 422

    async def test_user_isolation_404(self, client: AsyncClient) -> None:
        plan = await _create_plan(client)
        r = await client.get(f"/api/v1/study/plans/{plan['plan_id']}", headers=auth(USER_B))
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "STUDY_PLAN_NOT_FOUND"
