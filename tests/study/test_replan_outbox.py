"""Study Replan 与 Outbox 集成测试（§9.4/§12.2/§14/D18/D21，§20.3，Phase 4）。"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text

from tests.study.conftest import USER_A, auth, manual_plan_body

START = "2026-08-17"  # 周一（未来）


async def _plan_with_overdue_task(
    client: AsyncClient, study_session_factory: Any
) -> tuple[dict[str, Any], str]:
    """激活计划并把首个任务改成 overdue（scheduled_date < 今天）。"""
    r = await client.post(
        "/api/v1/study/plans",
        json=manual_plan_body(start_date=START, duration_weeks=6),
        headers={**auth(USER_A), "Idempotency-Key": "rp-plan-1"},
    )
    plan = r.json()
    await client.post(
        f"/api/v1/study/plans/{plan['plan_id']}/activate",
        json={"expected_version": 1},
        headers={**auth(USER_A), "Idempotency-Key": "rp-act-1"},
    )
    async with study_session_factory() as session:
        async with session.begin():
            task_id = (
                await session.execute(
                    text(
                        "SELECT task_id FROM study_tasks WHERE plan_id = :pid "
                        "ORDER BY scheduled_date LIMIT 1"
                    ),
                    {"pid": plan["plan_id"]},
                )
            ).scalar_one()
            await session.execute(
                text("UPDATE study_tasks SET scheduled_date = :d WHERE task_id = :tid"),
                {"d": date.today() - timedelta(days=3), "tid": task_id},
            )
    return plan, str(task_id)


class TestReplanAdjustments:
    async def test_user_adjustment_moves_overdue_task_and_auto_activates(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        plan, _task_id = await _plan_with_overdue_task(client, study_session_factory)
        r = await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/adjustments",
            json={"expected_version": 2},
            headers={**auth(USER_A), "Idempotency-Key": "adj-1"},
        )
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["status"] in ("succeeded", "needs_input")
        # 新 revision 的同一任务已顺延到未来非休息日
        revisions = (
            await client.get(
                f"/api/v1/study/plans/{plan['plan_id']}/revisions", headers=auth(USER_A)
            )
        ).json()
        latest = revisions[0]
        assert latest["revision_no"] == 2
        if body["status"] == "succeeded":
            assert latest["status"] == "active"
            async with study_session_factory() as session:
                rows = (
                    (
                        await session.execute(
                            text("SELECT scheduled_date FROM study_tasks WHERE revision_id = :rid"),
                            {"rid": latest["revision_id"]},
                        )
                    )
                    .scalars()
                    .all()
                )
            assert all(d >= date.today() for d in rows)

    async def test_outbox_event_on_activate_with_writeback_flag(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        from tests.study.conftest import make_study_client

        async with await make_study_client(
            study_session_factory, study_memory_writeback_enabled=True
        ) as wb_client:
            r = await wb_client.post(
                "/api/v1/study/plans",
                json=manual_plan_body(start_date=START, duration_weeks=6),
                headers={**auth(USER_A), "Idempotency-Key": "wb-plan-1"},
            )
            plan = r.json()
            r = await wb_client.post(
                f"/api/v1/study/plans/{plan['plan_id']}/activate",
                json={"expected_version": 1},
                headers={**auth(USER_A), "Idempotency-Key": "wb-act-1"},
            )
            assert r.status_code == 200
        async with study_session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT event_type, idempotency_key FROM study_outbox "
                            "WHERE user_id = :uid"
                        ),
                        {"uid": USER_A},
                    )
                )
                .mappings()
                .all()
            )
        assert any(r["event_type"] == "study.plan_activated" for r in rows)

    async def test_outbox_event_on_task_complete_with_writeback(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        from tests.study.conftest import make_study_client

        async with await make_study_client(
            study_session_factory, study_memory_writeback_enabled=True
        ) as wb_client:
            r = await wb_client.post(
                "/api/v1/study/plans",
                json=manual_plan_body(start_date=START, duration_weeks=6),
                headers={**auth(USER_A), "Idempotency-Key": "wb-plan-2"},
            )
            plan = r.json()
            await wb_client.post(
                f"/api/v1/study/plans/{plan['plan_id']}/activate",
                json={"expected_version": 1},
                headers={**auth(USER_A), "Idempotency-Key": "wb-act-2"},
            )
            calendar = (
                await wb_client.get(
                    f"/api/v1/study/plans/{plan['plan_id']}/calendar", headers=auth(USER_A)
                )
            ).json()
            task = calendar["weeks"][0]["days"][0]["tasks"][0]
            await wb_client.post(
                f"/api/v1/study/tasks/{task['task_id']}/complete",
                json={"expected_version": 1},
                headers={**auth(USER_A), "Idempotency-Key": "wb-complete-1"},
            )
        async with study_session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text("SELECT event_type FROM study_outbox WHERE user_id = :uid"),
                        {"uid": USER_A},
                    )
                )
                .scalars()
                .all()
            )
        assert "study.task_completed" in rows
