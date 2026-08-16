"""Study 首页与内部 purge API 集成测试（§12.6/§12.8/D19/D22/D29，§20.3）。"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient
from sqlalchemy import text

from tests.study.conftest import USER_A, auth, manual_plan_body, system_auth

START = "2026-08-17"


async def _active_plan(client: AsyncClient) -> dict[str, Any]:
    r = await client.post(
        "/api/v1/study/plans",
        json=manual_plan_body(start_date=START, duration_weeks=6),
        headers={**auth(USER_A), "Idempotency-Key": "p1"},
    )
    plan = r.json()
    await client.post(
        f"/api/v1/study/plans/{plan['plan_id']}/activate",
        json={"expected_version": 1},
        headers={**auth(USER_A), "Idempotency-Key": "act1"},
    )
    return plan


class TestHome:
    async def test_no_plan_returns_no_active_plan(self, client: AsyncClient) -> None:
        r = await client.get("/api/v1/study/home", headers=auth(USER_A))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["active_plan"] is None
        assert body["today"]["generation_status"] == "no_active_plan"
        assert len(body["recent_7_days"]["days"]) == 7
        assert body["recent_7_days"]["total_active_minutes"] == 0

    async def test_home_with_active_plan(self, client: AsyncClient) -> None:
        await _active_plan(client)
        r = await client.get("/api/v1/study/home", headers=auth(USER_A))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["timezone"] == "Asia/Shanghai"
        assert body["active_plan"] is not None
        assert body["active_plan"]["personalization_status"] == "not_requested"
        assert body["active_plan"]["progress_percent"] == 0
        assert body["today"]["generation_status"] == "pending"
        assert len(body["recent_7_days"]["days"]) == 7
        assert body["recent_7_days"]["days"][-1]["local_date"] == body["local_date"]

    async def test_future_date_rejected_422(self, client: AsyncClient) -> None:
        await _active_plan(client)
        r = await client.get("/api/v1/study/home?date=2999-01-01", headers=auth(USER_A))
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_PAYLOAD"

    async def test_home_after_complete_updates_progress(self, client: AsyncClient) -> None:
        plan = await _active_plan(client)
        calendar = (
            await client.get(
                f"/api/v1/study/plans/{plan['plan_id']}/calendar", headers=auth(USER_A)
            )
        ).json()
        task = calendar["weeks"][0]["days"][0]["tasks"][0]
        await client.post(
            f"/api/v1/study/tasks/{task['task_id']}/complete",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "c1"},
        )
        r = await client.get("/api/v1/study/home", headers=auth(USER_A))
        body = r.json()
        assert body["active_plan"]["progress_percent"] == 100
        assert body["active_plan"]["workload_progress_percent"] == 100


class TestPurge:
    async def test_purge_requires_system_principal(self, client: AsyncClient) -> None:
        await _active_plan(client)
        payload = {
            "account_deletion_id": "33333333-3333-4333-8333-333333333333",
            "user_id": USER_A,
            "requested_at": "2026-08-16T00:00:00Z",
        }
        # 普通用户 → 403
        r = await client.post(
            "/api/v1/internal/study-accounts/purge", json=payload, headers=auth(USER_A)
        )
        assert r.status_code == 403

    async def test_purge_idempotent_and_complete(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        await _active_plan(client)
        payload = {
            "account_deletion_id": "33333333-3333-4333-8333-333333333333",
            "user_id": USER_A,
            "requested_at": "2026-08-16T00:00:00Z",
        }
        headers = system_auth()
        r1 = await client.post(
            "/api/v1/internal/study-accounts/purge", json=payload, headers=headers
        )
        assert r1.status_code == 200, r1.text
        assert r1.json()["status"] == "succeeded"
        # 幂等重放
        r2 = await client.post(
            "/api/v1/internal/study-accounts/purge", json=payload, headers=headers
        )
        assert r2.status_code == 200
        assert r2.json()["status"] == "succeeded"
        # 数据已清空：计划列表为空
        r = await client.get("/api/v1/study/plans", headers=auth(USER_A))
        assert r.json() == []
        # 删除账本保留
        async with study_session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT status FROM study_account_purge_ledger "
                        "WHERE account_deletion_id = :did"
                    ),
                    {"did": payload["account_deletion_id"]},
                )
            ).scalar_one()
        assert row == "succeeded"
