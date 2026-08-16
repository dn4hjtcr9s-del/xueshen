"""评审收口修复的回归测试（Critical 5 + 必改项 + 测试体系空洞）。

覆盖：reopen 清空字段、adjustment_required 信封、revision 决策带 operation 终态、
adjust_plan 不产生重复 revision、并发 ensure-today 单 run、幂等并发不 500、
模型缓存 stale running 恢复与命中零调用、保留期清理、purge 全表覆盖。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from httpx import AsyncClient
from sqlalchemy import text

from tests.study.conftest import USER_A, auth, make_study_client, manual_plan_body

START = "2026-08-17"


async def _plan_with_task(client: AsyncClient) -> tuple[dict[str, Any], dict[str, Any]]:
    r = await client.post(
        "/api/v1/study/plans",
        json=manual_plan_body(start_date=START, duration_weeks=6),
        headers={**auth(USER_A), "Idempotency-Key": "rf-plan-1"},
    )
    plan = r.json()
    await client.post(
        f"/api/v1/study/plans/{plan['plan_id']}/activate",
        json={"expected_version": 1},
        headers={**auth(USER_A), "Idempotency-Key": "rf-act-1"},
    )
    calendar = (
        await client.get(f"/api/v1/study/plans/{plan['plan_id']}/calendar", headers=auth(USER_A))
    ).json()
    task = calendar["weeks"][0]["days"][0]["tasks"][0]
    return plan, task


class TestReopenClearsCompletedFields:
    async def test_reopen_clears_completed_at_and_source(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        """Critical 3：reopen 必须清除 completed_at/completion_source。"""
        _plan, task = await _plan_with_task(client)
        tid = task["task_id"]
        await client.post(
            f"/api/v1/study/tasks/{tid}/complete",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "rf-c1"},
        )
        r = await client.post(
            f"/api/v1/study/tasks/{tid}/reopen",
            json={"expected_version": 2},
            headers={**auth(USER_A), "Idempotency-Key": "rf-r1"},
        )
        assert r.status_code == 200, r.text
        async with study_session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT completed_at, completion_source FROM study_tasks "
                            "WHERE task_id = :tid"
                        ),
                        {"tid": tid},
                    )
                )
                .mappings()
                .first()
            )
        assert row["completed_at"] is None
        assert row["completion_source"] is None


class TestScheduleConflictEnvelope:
    async def test_reschedule_conflict_has_adjustment_required(self, client: AsyncClient) -> None:
        """必改 #5：SCHEDULE_CONFLICT 响应带 adjustment_required=true。"""
        _plan, task = await _plan_with_task(client)
        r = await client.post(
            f"/api/v1/study/tasks/{task['task_id']}/reschedule",
            json={"scheduled_date": "2026-08-18", "expected_version": 1},  # 休息日
            headers={**auth(USER_A), "Idempotency-Key": "rf-rs1"},
        )
        assert r.status_code == 409
        body = r.json()["error"]
        assert body["code"] == "STUDY_SCHEDULE_CONFLICT"
        assert body["adjustment_required"] is True


class TestRevisionDecisionOperation:
    async def test_accept_returns_operation_terminal_state(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        """必改 #6：§12.2 决策响应包含关联 operation 终态。"""
        plan, _task = await _plan_with_task(client)
        # 直接制造一个带 needs_input operation 的 proposed revision
        from uuid import uuid4

        op_id = uuid4()
        revision_id = uuid4()
        async with study_session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO study_operations (operation_id, user_id, operation_type,
                            payload, status)
                        VALUES (:op, :uid, 'replan', '{}', 'needs_input')
                        """
                    ),
                    {"op": op_id, "uid": USER_A},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO study_plan_revisions (revision_id, plan_id, revision_no,
                            reason, status, input_snapshot, proposal_operation_id,
                            base_revision_id, personalization_status)
                        VALUES (:rid, :pid, 2, 'weekly_replan', 'proposed', '{}', :op,
                            :base, 'not_requested')
                        """
                    ),
                    {
                        "rid": revision_id,
                        "pid": plan["plan_id"],
                        "op": op_id,
                        "base": plan["current_revision_id"],
                    },
                )
        r = await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/revisions/{revision_id}/reject",
            json={"expected_version": 2, "reason": "暂不调整"},
            headers={**auth(USER_A), "Idempotency-Key": "rf-rej-1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["revision"]["status"] == "rejected"
        assert body["operation"]["status"] == "cancelled"


class TestAdjustPlanNoDuplicates:
    async def test_adjust_plan_leaves_no_queued_operation(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        """Critical 1：adjust_plan 内联执行后 operation 终态化，worker 不会再消费。"""
        from backend.study.worker.main import _claim_batch

        plan, _task = await _plan_with_task(client)
        r = await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/adjustments",
            json={"expected_version": 2},
            headers={**auth(USER_A), "Idempotency-Key": "rf-adj-1"},
        )
        assert r.status_code == 202, r.text
        async with study_session_factory() as session:
            queued = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM study_operations WHERE status = 'queued' "
                        "AND user_id = :uid"
                    ),
                    {"uid": USER_A},
                )
            ).scalar_one()
            revisions = (
                await session.execute(
                    text("SELECT COUNT(*) FROM study_plan_revisions WHERE plan_id = :pid"),
                    {"pid": plan["plan_id"]},
                )
            ).scalar_one()
        assert queued == 0
        assert revisions == 2  # 只多一个 replan revision
        # worker claim 拿不到任何东西（无二次执行）
        claimed = await _claim_batch(
            study_session_factory, worker_id="t", lease_seconds=60, batch_size=10
        )
        assert claimed == []


class TestConcurrentEnsureToday:
    async def test_concurrent_ensure_creates_single_run(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        """必改 #2：唯一键并发——两个并发 ensure-today 只产生一个 run/operation。"""
        r = await client.post(
            "/api/v1/study/plans",
            json=manual_plan_body(start_date=START, duration_weeks=6),
            headers={**auth(USER_A), "Idempotency-Key": "rf-plan-2"},
        )
        plan = r.json()
        await client.post(
            f"/api/v1/study/plans/{plan['plan_id']}/activate",
            json={"expected_version": 1},
            headers={**auth(USER_A), "Idempotency-Key": "rf-act-2"},
        )
        client2 = await make_study_client(study_session_factory)
        try:
            async with client2:
                (r1, r2) = await asyncio.gather(
                    client.post(
                        "/api/v1/study/home/ensure-today",
                        headers={**auth(USER_A), "Idempotency-Key": "cc-1"},
                    ),
                    client2.post(
                        "/api/v1/study/home/ensure-today",
                        headers={**auth(USER_A), "Idempotency-Key": "cc-2"},
                    ),
                )
        finally:
            pass
        assert r1.status_code in (200, 202), r1.text
        assert r2.status_code in (200, 202), r2.text
        async with study_session_factory() as session:
            runs = (
                (
                    await session.execute(
                        text(
                            "SELECT feed_run_id, operation_id FROM study_daily_feed_runs "
                            "WHERE user_id = :uid"
                        ),
                        {"uid": USER_A},
                    )
                )
                .mappings()
                .all()
            )
            operations = (
                await session.execute(
                    text(
                        "SELECT COUNT(*) FROM study_operations WHERE user_id = :uid "
                        "AND operation_type = 'daily_feed_generation'"
                    ),
                    {"uid": USER_A},
                )
            ).scalar_one()
        assert len(runs) == 1
        assert operations == 1  # 孤儿 operation 竞态不成立（ON CONFLICT 等待后复用）


class TestIdempotencyConcurrencyRecovery:
    async def test_integrity_error_rollback_then_replay(self, study_session_factory: Any) -> None:
        """Critical 5：幂等唯一键冲突后 session 不 aborted，重查可正常继续。"""
        from backend.study.services.idempotency import open_idempotent_request

        now = datetime.now(UTC)
        async with study_session_factory() as session:
            first = await open_idempotent_request(
                session,
                user_id=UUID(USER_A),
                operation_name="op.x",
                idempotency_key="same-key",
                payload={"a": 1},
                now=now,
                retention_days=7,
            )
            assert first is not None and first.replay is False
            await session.commit()
        # 并发同键：直接再插一次（模拟竞争失败路径）
        from backend.study.persistence import repositories as repo

        async with study_session_factory() as session:
            inserted = await repo.insert_idempotency_row(
                session,
                idempotency_request_id=uuid4(),
                user_id=UUID(USER_A),
                operation_name="op.x",
                idempotency_key="same-key",
                request_hash="abc",
                expires_at=now + timedelta(days=7),
            )
            assert inserted is False
            # 回滚后 session 仍可用：重查返回既有行
            existing = await repo.get_idempotency_row(
                session, user_id=UUID(USER_A), operation_name="op.x", idempotency_key="same-key"
            )
            assert existing is not None
            # 同 payload 重放 → 未完成状态继续执行（不是 500）
            reopened = await open_idempotent_request(
                session,
                user_id=UUID(USER_A),
                operation_name="op.x",
                idempotency_key="same-key",
                payload={"a": 1},
                now=now,
                retention_days=7,
            )
            assert reopened is not None and reopened.replay is False
            await session.commit()


class TestModelCacheRecovery:
    async def test_stale_running_row_recovers_and_hit_skips_client(
        self, study_session_factory: Any, monkeypatch: Any
    ) -> None:
        """必改 #1：stale running 行可回收；命中缓存后 OpenAI 调用次数为零。"""
        from backend.settings import Settings
        from backend.study.gateways.openai import StudyOpenAIGateway

        class FakeResponses:
            def __init__(self) -> None:
                self.calls = 0

            @property
            def output_text(self) -> str:
                return (
                    '{"tasks": [{"title": "任务", "task_type": "learn", "estimated_minutes": 30}]}'
                )

            @property
            def usage(self) -> None:
                return None

        class FakeResponsesApi:
            def __init__(self) -> None:
                self.created: list[Any] = []

            async def create(self, **kwargs: Any) -> FakeResponses:
                self.created.append(kwargs)
                return FakeResponses()

        settings = Settings(
            app_env="test",
            openai_api_key="sk-fake",
            openai_study_plan_model="fake-plan",
            openai_study_intake_model="fake-intake",
            openai_study_feed_model="fake-feed",
            _env_file=None,
        )
        gateway = StudyOpenAIGateway(settings=settings)
        fake_api = FakeResponsesApi()
        monkeypatch.setattr(gateway, "_client", SimpleClient(fake_api))

        from backend.study.contracts.graph import PlanBlueprint

        now = datetime.now(UTC)
        # 第一次调用：落库 running 行后模拟崩溃（不完成）
        async with study_session_factory() as session:
            from backend.study.gateways.openai import input_hash_of
            from backend.study.persistence import repositories as repo

            model = "fake-plan"
            h = input_hash_of("plan", "plan-v1", "1", "sys", {"x": 1})
            await repo.insert_model_call_row(
                session,
                model_call_id=uuid4(),
                user_id=UUID(USER_A),
                operation_id=None,
                purpose="plan",
                input_hash=h,
                prompt_version="plan-v1",
                model=model,
                schema_version="1",
                expires_at=now + timedelta(days=30),
            )
            # 置为很久以前的 running（进程崩溃遗留）
            await session.execute(
                text(
                    "UPDATE study_model_call_records SET created_at = now() - interval '1 hour' "
                    "WHERE user_id = :uid"
                ),
                {"uid": USER_A},
            )
            await session.commit()
            # 第二次调用：stale running 被回收 → 真正调用 OpenAI（一次）
            result = await gateway.structured_call(
                session=session,
                user_id=UUID(USER_A),
                operation_id=None,
                purpose="plan",
                prompt_version="plan-v1",
                system_prompt="sys",
                user_payload={"x": 1},
                text_format=PlanBlueprint,
                cache_retention_days=30,
                now=now,
            )
            await session.commit()
            assert len(result.tasks) == 1
            assert len(fake_api.created) == 1
            # 第三次调用：缓存命中，不再调用 OpenAI
            result2 = await gateway.structured_call(
                session=session,
                user_id=UUID(USER_A),
                operation_id=None,
                purpose="plan",
                prompt_version="plan-v1",
                system_prompt="sys",
                user_payload={"x": 1},
                text_format=PlanBlueprint,
                cache_retention_days=30,
                now=now,
            )
            await session.commit()
            assert len(fake_api.created) == 1
            assert len(result2.tasks) == 1


class SimpleClient:
    """最小 AsyncOpenAI 替身（responses.create 委托 fake）。"""

    def __init__(self, fake_api: Any) -> None:
        self.responses = fake_api


class TestRetentionCleanup:
    async def test_scheduler_cleanup_removes_expired_rows(self, study_session_factory: Any) -> None:
        from backend.settings import Settings, get_settings
        from backend.study.persistence.database import StudyDatabase
        from backend.study.scheduler.main import _cleanup_retention

        settings = Settings(
            app_env="test",
            study_database_url=get_settings().study_database_url,
            _env_file=None,
        )
        db = StudyDatabase(settings)
        db.session_factory = study_session_factory  # type: ignore[assignment]
        async with study_session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO study_idempotency_requests
                            (idempotency_request_id, user_id, operation_name,
                             idempotency_key, request_hash, expires_at)
                        VALUES (gen_random_uuid(), :uid, 'op.x', 'expired-key', 'h',
                                now() - interval '1 day')
                        """
                    ),
                    {"uid": USER_A},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO study_model_call_records
                            (model_call_id, user_id, purpose, input_hash, prompt_version,
                             model, schema_version, expires_at)
                        VALUES (gen_random_uuid(), :uid, 'plan', 'h2', 'v1', 'm', '1',
                                now() - interval '31 days')
                        """
                    ),
                    {"uid": USER_A},
                )
        removed = await _cleanup_retention(db, settings, __import__("logging").getLogger("t"))
        assert removed == 2


class TestPurgeCoverage:
    async def test_purge_clears_all_study_tables(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        """§18.9/§20.3：purge 覆盖全部用户数据表（含幂等/缓存/feed/统计）。"""
        from tests.study.conftest import system_auth

        _plan, task = await _plan_with_task(client)
        # 制造各表数据
        async with study_session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO study_model_call_records
                            (model_call_id, user_id, purpose, input_hash, prompt_version,
                             model, schema_version, expires_at)
                        VALUES (gen_random_uuid(), :uid, 'plan', 'h1', 'v1', 'm', '1',
                                now() + interval '1 day')
                        """
                    ),
                    {"uid": USER_A},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO study_daily_stats (user_id, local_date, active_seconds)
                        VALUES (:uid, current_date, 60)
                        """
                    ),
                    {"uid": USER_A},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO study_sessions (session_id, user_id, task_id)
                        VALUES (gen_random_uuid(), :uid, :tid)
                        """
                    ),
                    {"uid": USER_A, "tid": task["task_id"]},
                )
        payload = {
            "account_deletion_id": "55555555-5555-4555-8555-555555555555",
            "user_id": USER_A,
            "requested_at": "2026-08-16T00:00:00Z",
        }
        r = await client.post(
            "/api/v1/internal/study-accounts/purge", json=payload, headers=system_auth()
        )
        assert r.status_code == 200, r.text
        async with study_session_factory() as session:
            for table in (
                "study_plans",
                "study_tasks",
                "study_sessions",
                "study_daily_stats",
                "study_model_call_records",
                "study_idempotency_requests",
                "study_operations",
                "study_outbox",
                "study_plan_revisions",
                "study_plan_availability",
                "study_task_events",
                "study_daily_feed_runs",
                "study_daily_feed_items",
                "study_plan_intakes",
                "study_user_leases",
            ):
                count = (await session.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar_one()
                assert count == 0, f"{table} 未被清理"


class TestWorkerNeedsInputEndToEnd:
    async def test_replan_needs_input_survives_worker_terminal_write(
        self, study_session_factory: Any
    ) -> None:
        """Critical 2 端到端回归：worker 执行 replan 产出 needs_input，
        终态写入不得覆写为 succeeded。"""
        import logging
        from datetime import date, timedelta
        from uuid import uuid4

        from backend.settings import Settings
        from backend.study.worker.main import (
            _claim_batch,
            _finish_operation,
            _run_operation,
        )

        today = date.today()
        plan_id = uuid4()
        revision_id = uuid4()
        task_id = uuid4()
        op_id = uuid4()
        async with study_session_factory() as session:
            async with session.begin():
                # 活跃计划：今天为休息日（不可顺延），任务已过期 → 不可行 → major
                await session.execute(
                    text(
                        """
                        INSERT INTO study_plans (plan_id, user_id, goal, status, timezone,
                            start_date, target_date, weekly_minutes, session_min_minutes,
                            session_max_minutes, current_revision_id)
                        VALUES (:pid, :uid, '目标', 'active', 'Asia/Shanghai', :start,
                            :target, 60, 15, 60, :rid)
                        """
                    ),
                    {
                        "pid": plan_id,
                        "uid": USER_A,
                        "start": today - timedelta(days=1),
                        "target": today,
                        "rid": revision_id,
                    },
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO study_plan_availability (plan_id, day_of_week,
                            available_minutes, is_rest_day)
                        VALUES (:pid, :dow, 0, true)
                        """
                    ),
                    {"pid": plan_id, "dow": today.isoweekday()},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO study_plan_revisions (revision_id, plan_id, revision_no,
                            reason, status, input_snapshot, personalization_status)
                        VALUES (:rid, :pid, 1, 'initial', 'active', '{}', 'not_requested')
                        """
                    ),
                    {"rid": revision_id, "pid": plan_id},
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO study_tasks (task_id, plan_id, revision_id,
                            scheduled_date, order_index, task_type, title,
                            estimated_minutes, source, status)
                        VALUES (:tid, :pid, :rid, :d, 1, 'learn', '过期任务', 40,
                                'plan', 'pending')
                        """
                    ),
                    {
                        "tid": task_id,
                        "pid": plan_id,
                        "rid": revision_id,
                        "d": today - timedelta(days=1),
                    },
                )
                await session.execute(
                    text(
                        """
                        INSERT INTO study_operations (operation_id, user_id, operation_type,
                            payload, status)
                        VALUES (:op, :uid, 'replan', :payload, 'queued')
                        """
                    ),
                    {
                        "op": op_id,
                        "uid": USER_A,
                        "payload": __import__("json").dumps(
                            {
                                "plan_id": str(plan_id),
                                "reason": "weekly_replan",
                                "user_requested": False,
                            }
                        ),
                    },
                )
        settings = Settings(app_env="test", _env_file=None)
        claimed = await _claim_batch(
            study_session_factory, worker_id="w1", lease_seconds=60, batch_size=5
        )
        assert len(claimed) == 1
        result = await _run_operation(
            operation=claimed[0],
            session_factory=study_session_factory,
            graphs={},
            worker_id="w1",
            settings=settings,
            logger=logging.getLogger("t"),
        )
        assert result["status"] == "needs_input"
        await _finish_operation(
            study_session_factory,
            operation_id=op_id,
            status=str(result["status"]),
            result_payload=result,
        )
        async with study_session_factory() as session:
            status = (
                await session.execute(
                    text("SELECT status FROM study_operations WHERE operation_id = :op"),
                    {"op": op_id},
                )
            ).scalar_one()
        assert status == "needs_input"
