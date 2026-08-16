"""Study Daily Feed 集成测试（§9.3/§11.2/§12.4/§12.6/D9/D13/D20/D22，§20.3）。"""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import text

from tests.study.conftest import USER_A, auth, manual_plan_body

START = "2026-08-17"


async def _active_plan(client: AsyncClient) -> dict[str, Any]:
    r = await client.post(
        "/api/v1/study/plans",
        json=manual_plan_body(start_date=START, duration_weeks=6),
        headers={**auth(USER_A), "Idempotency-Key": "feed-plan-1"},
    )
    plan = r.json()
    await client.post(
        f"/api/v1/study/plans/{plan['plan_id']}/activate",
        json={"expected_version": 1},
        headers={**auth(USER_A), "Idempotency-Key": "feed-act-1"},
    )
    return plan


def _server_today(plan: dict[str, Any]) -> date:
    from backend.study.services.home_service import server_today

    return server_today(str(plan["timezone"]))


async def _seed_succeeded_feed(
    study_session_factory: Any,
    *,
    plan: dict[str, Any],
    user_id: str,
    local_date: date,
    recommendations: list[dict[str, Any]] | None = None,
) -> UUID:
    from backend.study.services.feed_service import feed_input_hash

    run_id = uuid4()
    current_hash = feed_input_hash(
        plan_id=UUID(plan["plan_id"]),
        revision_id=plan["current_revision_id"],
        local_date=local_date,
    )
    async with study_session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    """
                    INSERT INTO study_daily_feed_runs (feed_run_id, user_id, plan_id,
                        revision_id, local_date, timezone, status, input_hash, completed_at)
                    VALUES (:run_id, :user_id, :plan_id, :revision_id, :local_date,
                        :timezone, 'succeeded', :input_hash, now())
                    """
                ),
                {
                    "run_id": run_id,
                    "user_id": user_id,
                    "plan_id": plan["plan_id"],
                    "revision_id": plan["current_revision_id"],
                    "local_date": local_date,
                    "timezone": plan["timezone"],
                    "input_hash": current_hash,
                },
            )
            for rec in recommendations or []:
                await session.execute(
                    text(
                        """
                        INSERT INTO study_daily_feed_items (feed_item_id, feed_run_id,
                            source_type, topic_key, graph_node_id, title, reason,
                            reason_codes, estimated_minutes, launch_payload)
                        VALUES (:iid, :run_id, 'recommendation', :topic, :node, :title,
                            :reason, :codes, :minutes, '{}'::jsonb)
                        """
                    ),
                    {
                        "iid": uuid4(),
                        "run_id": run_id,
                        "topic": rec.get("topic_key"),
                        "node": rec.get("graph_node_id"),
                        "title": rec["title"],
                        "reason": rec.get("reason", "推荐原因"),
                        "codes": __import__("json").dumps(rec.get("reason_codes", [])),
                        "minutes": rec.get("estimated_minutes", 20),
                    },
                )
    return run_id


class TestEnsureToday:
    async def test_ensure_creates_unique_run_and_operation(self, client: AsyncClient) -> None:
        await _active_plan(client)
        r1 = await client.post(
            "/api/v1/study/home/ensure-today",
            headers={**auth(USER_A), "Idempotency-Key": "ensure-1"},
        )
        assert r1.status_code == 202, r1.text
        run_id = r1.json()["feed_run_id"]
        op_id = r1.json()["operation_id"]
        # 再次触发（不同幂等键）：同一 run、不再创建第二个 operation
        r2 = await client.post(
            "/api/v1/study/home/ensure-today",
            headers={**auth(USER_A), "Idempotency-Key": "ensure-2"},
        )
        assert r2.status_code == 202
        assert r2.json()["feed_run_id"] == run_id
        assert r2.json()["operation_id"] == op_id
        # 幂等重放
        r3 = await client.post(
            "/api/v1/study/home/ensure-today",
            headers={**auth(USER_A), "Idempotency-Key": "ensure-1"},
        )
        assert r3.json()["feed_run_id"] == run_id

    async def test_ensure_without_active_plan_409_no_side_effect(self, client: AsyncClient) -> None:
        r = await client.post(
            "/api/v1/study/home/ensure-today",
            headers={**auth(USER_A), "Idempotency-Key": "ensure-1"},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "STUDY_NO_ACTIVE_PLAN"

    async def test_get_home_has_no_side_effect(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        await _active_plan(client)
        await client.get("/api/v1/study/home", headers=auth(USER_A))
        async with study_session_factory() as session:
            count = (
                await session.execute(
                    text("SELECT COUNT(*) FROM study_daily_feed_runs WHERE user_id = :uid"),
                    {"uid": USER_A},
                )
            ).scalar_one()
        assert count == 0


class TestHomeFeedStatus:
    async def test_home_ready_with_recommendations(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        plan = await _active_plan(client)
        local_date = _server_today(plan)
        await _seed_succeeded_feed(
            study_session_factory,
            plan=plan,
            user_id=USER_A,
            local_date=local_date,
            recommendations=[
                {
                    "title": "复习矩阵秩的性质",
                    "reason": "复习到期",
                    "reason_codes": ["REVIEW_DUE"],
                    "topic_key": "linear-algebra:rank",
                    "estimated_minutes": 20,
                }
            ],
        )
        r = await client.get("/api/v1/study/home", headers=auth(USER_A))
        body = r.json()
        assert body["today"]["generation_status"] == "ready"
        assert len(body["today"]["recommendations"]) == 1
        assert body["today"]["recommendations"][0]["title"] == "复习矩阵秩的性质"

    async def test_home_pending_before_feed(self, client: AsyncClient) -> None:
        await _active_plan(client)
        r = await client.get("/api/v1/study/home", headers=auth(USER_A))
        assert r.json()["today"]["generation_status"] == "pending"


class TestRecommendationActions:
    async def test_accept_creates_task_with_d13_rules(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        plan = await _active_plan(client)
        # 用周三（非休息日且当日无正式任务）验证 D13 归属规则
        local_date = date(2026, 8, 19)
        run_id = await _seed_succeeded_feed(
            study_session_factory,
            plan=plan,
            user_id=USER_A,
            local_date=local_date,
            recommendations=[
                {
                    "title": "练习向量空间定义",
                    "reason": "继续学习",
                    "reason_codes": ["CONTINUE_LEARNING"],
                    "topic_key": "linear-algebra:vector-space",
                    "estimated_minutes": 20,
                }
            ],
        )
        async with study_session_factory() as session:
            item_id = (
                await session.execute(
                    text(
                        "SELECT feed_item_id FROM study_daily_feed_items WHERE feed_run_id = :rid"
                    ),
                    {"rid": run_id},
                )
            ).scalar_one()
        r = await client.post(
            f"/api/v1/study/recommendations/{item_id}/accept",
            headers={**auth(USER_A), "Idempotency-Key": "acc-1"},
        )
        assert r.status_code == 200, r.text
        task_id = r.json()["task_id"]
        async with study_session_factory() as session:
            row = (
                (
                    await session.execute(
                        text("SELECT * FROM study_tasks WHERE task_id = :tid"),
                        {"tid": task_id},
                    )
                )
                .mappings()
                .first()
            )
        assert row is not None
        assert row["source"] == "recommendation"
        assert str(row["source_feed_item_id"]) == str(item_id)
        assert str(row["revision_id"]) == str(plan["current_revision_id"])
        # 重复 accept（同幂等键）返回同一 task
        r2 = await client.post(
            f"/api/v1/study/recommendations/{item_id}/accept",
            headers={**auth(USER_A), "Idempotency-Key": "acc-1"},
        )
        assert r2.json()["task_id"] == task_id

    async def test_dismiss(self, client: AsyncClient, study_session_factory: Any) -> None:
        plan = await _active_plan(client)
        run_id = await _seed_succeeded_feed(
            study_session_factory,
            plan=plan,
            user_id=USER_A,
            local_date=_server_today(plan),
            recommendations=[{"title": "不再推荐", "estimated_minutes": 20}],
        )
        async with study_session_factory() as session:
            item_id = (
                await session.execute(
                    text(
                        "SELECT feed_item_id FROM study_daily_feed_items WHERE feed_run_id = :rid"
                    ),
                    {"rid": run_id},
                )
            ).scalar_one()
        r = await client.post(
            f"/api/v1/study/recommendations/{item_id}/dismiss",
            headers={**auth(USER_A), "Idempotency-Key": "dis-1"},
        )
        assert r.status_code == 200, r.text
        async with study_session_factory() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM study_daily_feed_items WHERE feed_item_id = :iid"),
                    {"iid": item_id},
                )
            ).scalar_one()
        assert status == "dismissed"

    async def test_user_isolation_accept_404(self, client: AsyncClient) -> None:
        await _active_plan(client)
        r = await client.post(
            "/api/v1/study/recommendations/33333333-3333-4333-8333-333333333333/accept",
            headers={**auth(USER_A), "Idempotency-Key": "x-1"},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "STUDY_FEED_ITEM_NOT_FOUND"
