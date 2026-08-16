"""Study Intake 与 Plan Generation 集成测试（§9.1/§9.2/D10/D15/§16，§20.2/§20.3）。

Fake Study LLM Client（不依赖真实 OpenAI）：intake 抽取走 fake gateway；
plan generation 用 Memory 关闭 + 无 OpenAI 的降级模板路径验证确定性排期
与 operation 终态；模型响应缓存命中时第二次调用不经过 fake 的 call 计数。
"""

from __future__ import annotations

from typing import Any

from httpx import AsyncClient

from tests.study.conftest import USER_A, auth


class FakeIntakeGateway:
    """Fake：按 message 内容返回抽取结果（call_count 用于断言）。"""

    def __init__(self, extraction: Any) -> None:
        self._extraction = extraction
        self.call_count = 0

    def model_for(self, purpose: str) -> str:
        return "fake-intake-model"

    async def structured_call(self, **kwargs: Any) -> Any:
        self.call_count += 1
        return self._extraction


async def _create_intake(client: AsyncClient) -> dict[str, Any]:
    r = await client.post("/api/v1/study/intakes", headers=auth(USER_A))
    assert r.status_code == 201, r.text
    return r.json()


class TestIntakeFlow:
    async def test_create_intake_collecting(self, client: AsyncClient) -> None:
        intake = await _create_intake(client)
        assert intake["status"] == "collecting"
        assert intake["message_count"] == 0

    async def test_message_asks_clarifying_questions(
        self, client: AsyncClient, monkeypatch: Any
    ) -> None:
        from backend.study.contracts.graph import IntakeExtraction

        fake = FakeIntakeGateway(
            IntakeExtraction(
                intent_patch={"goal": "六周内掌握线性代数"},
                missing_fields=["start_date", "target_date", "timezone", "weekly_availability"],
                clarifying_questions=["你打算从哪一天开始，每周能学习哪几天？"],
                ready=False,
            )
        )
        monkeypatch.setattr("backend.study.api.intakes._gateway_for", lambda runtime: fake)
        intake = await _create_intake(client)
        r = await client.post(
            f"/api/v1/study/intakes/{intake['intake_id']}/messages",
            json={"message": "我想六周内掌握线性代数"},
            headers={**auth(USER_A), "Idempotency-Key": "m1"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "collecting"
        assert "每周能学习哪几天" in body["reply"]
        assert body["intake"]["message_count"] == 1
        assert fake.call_count == 1
        # 同幂等键重放：不重复调用模型
        r2 = await client.post(
            f"/api/v1/study/intakes/{intake['intake_id']}/messages",
            json={"message": "我想六周内掌握线性代数"},
            headers={**auth(USER_A), "Idempotency-Key": "m1"},
        )
        assert r2.status_code == 200
        assert fake.call_count == 1

    async def test_ready_then_confirm_creates_operation(
        self, client: AsyncClient, monkeypatch: Any
    ) -> None:
        from backend.study.contracts.graph import IntakeExtraction

        complete = {
            "goal": "六周内掌握线性代数基础",
            "start_date": "2026-08-17",
            "duration_weeks": 6,
            "timezone": "Asia/Shanghai",
            "weekly_availability": [
                {"day_of_week": 1, "available_minutes": 40},
                {"day_of_week": 3, "available_minutes": 40},
                {"day_of_week": 5, "available_minutes": 60},
            ],
            "session_min_minutes": 15,
            "session_max_minutes": 60,
        }
        fake = FakeIntakeGateway(
            IntakeExtraction(intent_patch=complete, missing_fields=[], ready=True)
        )
        monkeypatch.setattr("backend.study.api.intakes._gateway_for", lambda runtime: fake)
        intake = await _create_intake(client)
        r = await client.post(
            f"/api/v1/study/intakes/{intake['intake_id']}/messages",
            json={"message": "六周内掌握线性代数，每周一三五各学40/40/60分钟"},
            headers={**auth(USER_A), "Idempotency-Key": "m1"},
        )
        assert r.json()["status"] == "ready"
        r = await client.post(
            f"/api/v1/study/intakes/{intake['intake_id']}/confirm", headers=auth(USER_A)
        )
        assert r.status_code == 202, r.text
        operation_id = r.json()["operation_id"]
        # 重复 confirm 幂等返回同一 operation
        r2 = await client.post(
            f"/api/v1/study/intakes/{intake['intake_id']}/confirm", headers=auth(USER_A)
        )
        assert r2.json()["operation_id"] == operation_id


class TestPlanGeneration:
    async def test_degraded_template_generation_creates_draft_plan(
        self, client: AsyncClient, study_session_factory: Any
    ) -> None:
        """§16：Memory 关闭 + 无 OpenAI → 通用模板降级计划（不失败）。"""
        from uuid import UUID

        from sqlalchemy import text

        from backend.settings import Settings
        from backend.study.graph.builder import build_plan_generation_graph
        from backend.study.worker.main import _finish_operation, _run_operation

        settings = Settings(app_env="test", study_database_url="unused", _env_file=None)
        # 直接落库一个 queued operation（走 worker 内部执行函数）
        async with study_session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO study_operations
                            (operation_id, user_id, operation_type, payload)
                        VALUES ('44444444-4444-4444-8444-444444444444',
                                :uid, 'plan_generation', :payload)
                        """
                    ),
                    {
                        "uid": USER_A,
                        "payload": __import__("json").dumps(
                            {
                                "intent": {
                                    "goal": "六周内掌握线性代数基础",
                                    "start_date": "2026-08-17",
                                    "duration_weeks": 6,
                                    "timezone": "Asia/Shanghai",
                                    "weekly_availability": [
                                        {"day_of_week": 1, "available_minutes": 40},
                                        {"day_of_week": 3, "available_minutes": 40},
                                        {"day_of_week": 5, "available_minutes": 60},
                                    ],
                                    "session_min_minutes": 15,
                                    "session_max_minutes": 60,
                                }
                            },
                            ensure_ascii=False,
                        ),
                    },
                )
        graph = build_plan_generation_graph(
            settings=settings,
            session_factory=study_session_factory,
            openai_gateway=None,
            memory_gateway=None,
        )
        async with study_session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT * FROM study_operations WHERE operation_id = "
                            "'44444444-4444-4444-8444-444444444444'"
                        )
                    )
                )
                .mappings()
                .first()
            )
            assert row is not None
        result = await _run_operation(
            operation=dict(row),
            session_factory=study_session_factory,
            graphs={"plan_generation": graph},
            worker_id="test-worker",
            settings=settings,
            logger=__import__("logging").getLogger("test"),
        )
        assert result["plan_id"] is not None
        await _finish_operation(
            study_session_factory,
            operation_id=UUID("44444444-4444-4444-8444-444444444444"),
            status="succeeded",
            result_payload=result,
        )
        # 计划已生成：draft + not_requested（Memory 关闭）+ 3 个模板任务
        r = await client.get("/api/v1/study/plans", headers=auth(USER_A))
        plans = r.json()
        assert len(plans) == 1
        assert plans[0]["status"] == "draft"
        assert plans[0]["personalization_status"] == "not_requested"
        calendar = (
            await client.get(
                f"/api/v1/study/plans/{plans[0]['plan_id']}/calendar", headers=auth(USER_A)
            )
        ).json()
        tasks = [t for w in calendar["weeks"] for d in w["days"] for t in d["tasks"]]
        assert len(tasks) == 3
        # operation 终态
        r = await client.get(
            "/api/v1/study/operations/44444444-4444-4444-8444-444444444444",
            headers=auth(USER_A),
        )
        assert r.json()["status"] == "succeeded"
