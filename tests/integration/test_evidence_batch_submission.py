"""证据提交侧的批量门控集成测试（memory-rebuild §2.6 状态机第 1 步 / §5.8）。

真实 PostgreSQL + 真实 API：验证 `memory_batch_enabled` 开关与两种门控（最短沉淀 /
explicit_remember 豁免到下一个 0 点），以及幂等重放时"只提前门控、不推后"的规则。

这些断言必须打真实库：`next_run_at` 是**门控的唯一载体**（§2.6 决议"方案 A 修订版"
刻意不新增 eligible_at 列），算错了整条 nightly 批量链路都会错时。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app import create_app
from backend.memory.api.dependencies import ApiRuntime, _tighten_pending_batch_gate
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.services.memory_service import MemoryService
from backend.memory.worker.worker import Worker, WorkerConfig
from backend.settings import Settings

USER_ID = uuid4()
TZ = "Asia/Shanghai"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings(
        app_env="development",
        dev_auth_enabled=True,
        dev_auth_allow_scope_override=True,
        memory_storage_root=str(tmp_path / "storage"),
        memory_scheduler_timezone=TZ,
        **overrides,  # type: ignore[arg-type]
    )


def _api_client(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    runner: LocalLangGraphRunner,
) -> httpx.AsyncClient:
    runtime = ApiRuntime(
        settings=settings,
        session_factory=session_factory,
        memory_service=memory_service,
        runner=runner,
        gateway_worker=Worker(
            session_factory=session_factory,
            runner=runner,
            config=WorkerConfig(),
            worker_id="batch-it",
        ),
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, runtime=runtime)),
        base_url="http://memory-api-test",
    )


def _agent_auth() -> dict[str, str]:
    return {
        "X-Dev-User-Id": str(USER_ID),
        "X-Dev-Actor-Type": "conversation_agent",
        "X-Dev-Scopes": "memory:submit_evidence",
    }


def _event(trigger: str) -> dict[str, object]:
    return {
        "kind": "conversation_evidence",
        "thread_id": f"t-{trigger}",
        "message_ids": ["m1"],
        "trigger": trigger,
    }


async def _submit(client: httpx.AsyncClient, *, trigger: str, key: str) -> dict[str, object]:
    response = await client.post(
        "/api/v1/memory/events",
        json=_event(trigger),
        headers={"Idempotency-Key": key, **_agent_auth()},
    )
    assert response.status_code == 202, response.text
    return dict(response.json())


async def _row(
    session_factory: async_sessionmaker[AsyncSession], operation_id: str
) -> dict[str, object]:
    async with session_factory() as session:
        result = await session.execute(
            text(
                "SELECT status, next_run_at, batch_operation_id FROM memory_operations "
                "WHERE operation_id = :operation_id"
            ),
            {"operation_id": operation_id},
        )
        row = result.mappings().one()
    return dict(row)


@pytest.mark.asyncio
async def test_batch_flag_off_keeps_legacy_queued_path(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    runner: LocalLangGraphRunner,
) -> None:
    """关闭批量开关时证据仍是 queued，门控不起作用（逐条路径逐字不变）。"""
    settings = _settings(tmp_path, memory_batch_enabled=False)
    async with _api_client(settings, session_factory, memory_service, runner) as client:
        created = await _submit(client, trigger="turn_boundary", key="k-off")

    row = await _row(session_factory, str(created["operation_id"]))
    assert row["status"] == "queued"
    assert row["batch_operation_id"] is None
    # 默认 next_run_at = now()：立刻可被 Worker 领取
    assert row["next_run_at"] <= datetime.now(UTC) + timedelta(seconds=5)


@pytest.mark.asyncio
async def test_turn_boundary_evidence_waits_for_min_age(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    runner: LocalLangGraphRunner,
) -> None:
    """普通证据落 pending_batch，门控 = 提交时刻 + 最短沉淀时长（默认 6 小时）。"""
    settings = _settings(tmp_path, memory_batch_enabled=True, memory_evidence_min_age_hours=6)
    submitted_after = datetime.now(UTC)
    async with _api_client(settings, session_factory, memory_service, runner) as client:
        created = await _submit(client, trigger="turn_boundary", key="k-age")

    row = await _row(session_factory, str(created["operation_id"]))
    assert row["status"] == "pending_batch"
    gate = row["next_run_at"]
    assert isinstance(gate, datetime)
    assert submitted_after + timedelta(hours=6) <= gate <= datetime.now(UTC) + timedelta(hours=6)


@pytest.mark.asyncio
async def test_explicit_remember_is_exempt_and_gated_to_next_midnight(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    runner: LocalLangGraphRunner,
) -> None:
    """explicit_remember 不受沉淀时长约束（D5）：门控 = 下一个 0 点（本地时区）。"""
    settings = _settings(tmp_path, memory_batch_enabled=True, memory_evidence_min_age_hours=72)
    async with _api_client(settings, session_factory, memory_service, runner) as client:
        created = await _submit(client, trigger="explicit_remember", key="k-explicit")

    row = await _row(session_factory, str(created["operation_id"]))
    assert row["status"] == "pending_batch"
    gate = row["next_run_at"]
    assert isinstance(gate, datetime)
    now = datetime.now(UTC)
    # 72 小时沉淀被豁免：门控落在 24 小时以内
    assert gate <= now + timedelta(hours=24)
    assert gate > now
    local = gate.astimezone(ZoneInfo(TZ))
    assert (local.hour, local.minute, local.second) == (0, 0, 0)


@pytest.mark.asyncio
async def test_activity_evidence_also_enters_the_pool(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    runner: LocalLangGraphRunner,
) -> None:
    """行为证据与对话证据同池（§2.6：批量对"证据"生效，不区分子类）。"""
    settings = _settings(tmp_path, memory_batch_enabled=True)
    async with _api_client(settings, session_factory, memory_service, runner) as client:
        response = await client.post(
            "/api/v1/memory/events",
            json={
                "kind": "activity_evidence",
                "activity_type": "exercise_attempt",
                "activity_ids": ["a1"],
            },
            headers={
                "Idempotency-Key": "k-activity",
                "X-Dev-User-Id": str(USER_ID),
                "X-Dev-Actor-Type": "activity_agent",
                "X-Dev-Scopes": "memory:submit_evidence",
            },
        )
        assert response.status_code == 202, response.text
        operation_id = str(response.json()["operation_id"])

    row = await _row(session_factory, operation_id)
    assert row["status"] == "pending_batch"


@pytest.mark.asyncio
async def test_commands_are_not_put_into_the_evidence_pool(
    tmp_path: Path,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    runner: LocalLangGraphRunner,
) -> None:
    """用户命令（P0）绝不能被批量门控拦住——它们是"立刻生效"的语义。"""
    settings = _settings(tmp_path, memory_batch_enabled=True)
    async with _api_client(settings, session_factory, memory_service, runner) as client:
        response = await client.post(
            "/api/v1/memory/commands/correct",
            json={
                "kind": "correct_memory",
                "memory_id": "mastery:配方法",
                "expected_version": 1,
                "reason": "用户纠正",
                "replacement": {
                    "replacement_type": "mastery",
                    "topic_title": "配方法",
                    "overview": "已修正",
                },
            },
            headers={
                "Idempotency-Key": "k-command",
                "X-Dev-User-Id": str(USER_ID),
                # correct_memory 需要 memory:correct（backend/auth/context.py）
                "X-Dev-Scopes": "memory:correct",
            },
        )
        assert response.status_code in (200, 202), response.text
        operation_id = str(response.json()["operation_id"])

    row = await _row(session_factory, operation_id)
    assert row["status"] != "pending_batch"
    assert row["batch_operation_id"] is None


@pytest.mark.asyncio
async def test_tighten_gate_only_moves_it_earlier(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """幂等重放时门控只允许提前：推后的更新必须被丢弃（否则重试等于拖延处理）。"""
    operation_id = uuid4()
    base = datetime.now(UTC) + timedelta(hours=6)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO memory_operations ("
                    "  operation_id, user_id, actor_type, input_kind, operation_type,"
                    "  idempotency_key, idempotency_payload_hash, priority, status,"
                    "  payload, trace_id, graph_thread_id, occurred_at, max_attempts, next_run_at"
                    ") VALUES ("
                    "  :operation_id, :user_id, 'conversation_agent', 'evidence',"
                    "  'conversation_evidence', :key, :hash, 50, 'pending_batch',"
                    "  CAST(:payload AS jsonb), :trace, :thread, now(), 4, :gate"
                    ")"
                ),
                {
                    "operation_id": operation_id,
                    "user_id": USER_ID,
                    "key": f"tighten-{operation_id.hex[:8]}",
                    "hash": "0" * 64,
                    "payload": '{"kind": "conversation_evidence", "thread_id": "t",'
                    ' "message_ids": ["m1"], "trigger": "turn_boundary"}',
                    "trace": "t" * 32,
                    "thread": f"memory-op:{operation_id}",
                    "gate": base,
                },
            )
    existing = {"operation_id": operation_id, "status": "pending_batch"}

    # 更早的门控（explicit_remember 把它拉到下一个 0 点）→ 生效
    earlier = base - timedelta(hours=3)
    async with session_factory() as session:
        async with session.begin():
            await _tighten_pending_batch_gate(session, existing=existing, next_run_at=earlier)
    assert (await _row(session_factory, str(operation_id)))["next_run_at"] == earlier

    # 更晚的门控 → 必须被忽略
    later = base + timedelta(hours=3)
    async with session_factory() as session:
        async with session.begin():
            await _tighten_pending_batch_gate(session, existing=existing, next_run_at=later)
    assert (await _row(session_factory, str(operation_id)))["next_run_at"] == earlier


@pytest.mark.asyncio
async def test_tighten_gate_ignores_non_pending_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """已入批/已终态的行不受重放影响（批次归属与终态不能被幂等重放改写）。"""
    operation_id = uuid4()
    gate = datetime.now(UTC) + timedelta(hours=6)
    async with session_factory() as session:
        async with session.begin():
            await session.execute(
                text(
                    "INSERT INTO memory_operations ("
                    "  operation_id, user_id, actor_type, input_kind, operation_type,"
                    "  idempotency_key, idempotency_payload_hash, priority, status,"
                    "  payload, trace_id, graph_thread_id, occurred_at, max_attempts, next_run_at"
                    ") VALUES ("
                    "  :operation_id, :user_id, 'conversation_agent', 'evidence',"
                    "  'conversation_evidence', :key, :hash, 50, 'succeeded',"
                    "  CAST(:payload AS jsonb), :trace, :thread, now(), 4, :gate"
                    ")"
                ),
                {
                    "operation_id": operation_id,
                    "user_id": USER_ID,
                    "key": f"tighten-skip-{operation_id.hex[:8]}",
                    "hash": "0" * 64,
                    "payload": '{"kind": "conversation_evidence", "thread_id": "t",'
                    ' "message_ids": ["m1"], "trigger": "turn_boundary"}',
                    "trace": "t" * 32,
                    "thread": f"memory-op:{operation_id}",
                    "gate": gate,
                },
            )

    async with session_factory() as session:
        async with session.begin():
            await _tighten_pending_batch_gate(
                session,
                existing={"operation_id": operation_id, "status": "succeeded"},
                next_run_at=gate - timedelta(hours=1),
            )
    assert (await _row(session_factory, str(operation_id)))["next_run_at"] == gate
