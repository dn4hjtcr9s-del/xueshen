"""Gateway API 集成测试（§23.3 / §23.5）：真实 PostgreSQL + 临时文件系统。

覆盖：幂等持久化往返、P0 快速路径真实 Graph 执行、operation 查询/取消、
§11.6 commit 中取消仲裁（commit_started_at）与崩溃残留清理、
内部账号删除（identity mapping 解析 + manifest + Outbox 同事务）。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app import create_app
from backend.memory.api.dependencies import ApiRuntime
from backend.memory.contracts.operations import MemoryOperation, MemoryOperationResult
from backend.memory.graph.runner import LocalLangGraphRunner
from backend.memory.persistence import operations as ops_repo
from backend.memory.persistence.identity import IdentityMappingRepository
from backend.memory.services.memory_service import MemoryService
from backend.memory.worker.worker import Worker, WorkerConfig
from backend.settings import Settings

USER_ID = uuid4()
OTHER_USER_ID = uuid4()


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """覆盖 conftest：dev auth 需要 APP_ENV=development（§18.1）。"""
    return Settings(
        app_env="development",
        dev_auth_enabled=True,
        dev_auth_allow_scope_override=True,
        memory_storage_root=str(tmp_path / "storage"),
    )


@pytest.fixture()
def api_client(
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    memory_service: MemoryService,
    runner: LocalLangGraphRunner,
) -> httpx.AsyncClient:
    gateway_worker = Worker(
        session_factory=session_factory,
        runner=runner,
        config=WorkerConfig(),
        worker_id="gateway-it",
    )
    runtime = ApiRuntime(
        settings=settings,
        session_factory=session_factory,
        memory_service=memory_service,
        runner=runner,
        gateway_worker=gateway_worker,
    )
    app = create_app(settings, runtime=runtime)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://memory-api-test"
    )


def _auth(user_id: UUID = USER_ID, **extra: str) -> dict[str, str]:
    return {"X-Dev-User-Id": str(user_id), **extra}


#: 证据只接受内部 Agent（裁决 2026-08-12：用户不提交证据）
_AGENT_EXTRA = {"X-Dev-Actor-Type": "conversation_agent", "X-Dev-Scopes": "memory:submit_evidence"}


def _agent_auth(user_id: UUID = USER_ID) -> dict[str, str]:
    return _auth(user_id, **_AGENT_EXTRA)


EVENT_BODY = {
    "kind": "conversation_evidence",
    "thread_id": "thread-it-1",
    "message_ids": ["m1"],
    "trigger": "explicit_remember",
}


async def test_event_idempotency_roundtrip(
    api_client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    headers = {"Idempotency-Key": "it-k-1", **_agent_auth()}
    first = await api_client.post("/api/v1/memory/events", json=EVENT_BODY, headers=headers)
    assert first.status_code == 202
    operation_id = first.json()["operation_id"]

    # 重复同键同 payload → 原 operation
    replay = await api_client.post("/api/v1/memory/events", json=EVENT_BODY, headers=headers)
    assert replay.status_code == 202
    assert replay.json()["operation_id"] == operation_id

    # 同键不同 payload → 422
    conflict = await api_client.post(
        "/api/v1/memory/events",
        json={**EVENT_BODY, "thread_id": "thread-it-2"},
        headers=headers,
    )
    assert conflict.status_code == 422
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_PAYLOAD"

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    text("SELECT user_id, actor_type, priority, status FROM memory_operations"),
                )
            )
            .mappings()
            .one()
        )
    assert str(row["user_id"]) == str(USER_ID)
    assert row["actor_type"] == "conversation_agent"
    assert row["priority"] == 50
    assert row["status"] == "queued"


async def test_fast_path_override_learner_completes_200(
    api_client: httpx.AsyncClient,
) -> None:
    """P0 命令经真实 Graph 快速路径：2 秒内完成返回 200（§14.2 / §19.2）。"""
    response = await api_client.put(
        "/api/v1/memory/learner",
        json={"preferences": ["喜欢例题驱动"], "goals": ["期末 95"], "plans": []},
        headers={"Idempotency-Key": "it-k-learner", **_auth()},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "succeeded"
    assert body["operation_type"] == "override_learner_profile"
    assert body["mutations"][0]["memory_id"] == "learner"

    learner = await api_client.get("/api/v1/memory/learner", headers=_auth())
    assert learner.status_code == 200
    assert learner.json()["preferences"] == ["喜欢例题驱动"]

    # operation 查询仅本人可见（§18.4）
    polled = await api_client.get(
        f"/api/v1/memory/operations/{body['operation_id']}", headers=_auth()
    )
    assert polled.status_code == 200
    idor = await api_client.get(
        f"/api/v1/memory/operations/{body['operation_id']}", headers=_auth(OTHER_USER_ID)
    )
    assert idor.status_code == 404


async def test_cancel_queued_then_409(
    api_client: httpx.AsyncClient,
) -> None:
    created = await api_client.post(
        "/api/v1/memory/events",
        json=EVENT_BODY,
        headers={"Idempotency-Key": "it-k-cancel-src", **_agent_auth()},
    )
    operation_id = created.json()["operation_id"]
    cancelled = await api_client.post(
        f"/api/v1/memory/operations/{operation_id}/cancel",
        headers={"Idempotency-Key": "it-k-cancel", **_auth()},
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancelled_at"] is not None

    again = await api_client.post(
        f"/api/v1/memory/operations/{operation_id}/cancel",
        headers={"Idempotency-Key": "it-k-cancel-2", **_auth()},
    )
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "OPERATION_CANCEL_NOT_ALLOWED"


async def test_cancel_running_in_commit_returns_409(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§11.6（裁决 2026-08-11）：running 且 commit_started_at 置位 → 409。"""
    created = await api_client.post(
        "/api/v1/memory/events",
        json=EVENT_BODY,
        headers={"Idempotency-Key": "it-k-commit-src", **_agent_auth()},
    )
    assert created.status_code == 202
    operation_id = UUID(created.json()["operation_id"])

    async with session_factory() as session:
        async with session.begin():
            claimed = await ops_repo.claim_operation(
                session, worker_id="it-commit", lease_seconds=300
            )
        assert [r["operation_id"] for r in claimed] == [operation_id]
        generation = int(claimed[0]["lease_generation"])
        async with session.begin():
            marked = await ops_repo.mark_commit_started(
                session,
                operation_id=operation_id,
                expected_worker="it-commit",
                expected_generation=generation,
            )
            assert marked

    response = await api_client.post(
        f"/api/v1/memory/operations/{operation_id}/cancel",
        headers={"Idempotency-Key": "it-k-commit-cancel", **_auth()},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OPERATION_CANCEL_NOT_ALLOWED"

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT status, cancel_requested_at FROM memory_operations "
                        "WHERE operation_id = :operation_id"
                    ),
                    {"operation_id": operation_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["status"] == "running"
    assert row["cancel_requested_at"] is None  # 未退化为协作取消

    # 标记清除后同一 operation 可协作取消
    async with session_factory() as session:
        async with session.begin():
            cleared = await ops_repo.clear_commit_started(
                session,
                operation_id=operation_id,
                expected_worker="it-commit",
                expected_generation=generation,
            )
            assert cleared
    retry = await api_client.post(
        f"/api/v1/memory/operations/{operation_id}/cancel",
        headers={"Idempotency-Key": "it-k-commit-cancel-2", **_auth()},
    )
    assert retry.status_code == 202


class _BlockingRunner:
    """阻塞式 Runner：进入 run 后等待 release，用于模拟执行中状态。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(
        self, operation: MemoryOperation, *, fencing: dict[str, Any] | None = None
    ) -> MemoryOperationResult:
        self.started.set()
        await self.release.wait()
        now = datetime.now(UTC)
        return MemoryOperationResult(
            operation_id=operation.operation_id,
            status="succeeded",
            operation_type=operation.operation_type,
            created_at=operation.occurred_at,
            updated_at=now,
            completed_at=now,
        )


async def test_stale_commit_marker_cleared_on_execute_then_cooperative_cancel(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """§11.6：崩溃残留的 commit_started_at 在执行开始时被清除，之后可协作取消。"""
    created = await api_client.post(
        "/api/v1/memory/events",
        json=EVENT_BODY,
        headers={"Idempotency-Key": "it-k-stale-src", **_agent_auth()},
    )
    assert created.status_code == 202
    operation_id = UUID(created.json()["operation_id"])

    async with session_factory() as session:
        async with session.begin():
            claimed = await ops_repo.claim_operation(
                session, worker_id="it-stale", lease_seconds=300
            )
        assert [r["operation_id"] for r in claimed] == [operation_id]
        async with session.begin():
            # 模拟崩溃残留：running + commit_started_at 置位
            marked = await ops_repo.mark_commit_started(
                session,
                operation_id=operation_id,
                expected_worker="it-stale",
                expected_generation=int(claimed[0]["lease_generation"]),
            )
            assert marked

    runner = _BlockingRunner()
    worker = Worker(
        session_factory=session_factory,
        runner=runner,  # type: ignore[arg-type]
        config=WorkerConfig(),
        worker_id="it-stale",
    )
    task = asyncio.create_task(worker.execute_claimed(claimed[0]))
    await asyncio.wait_for(runner.started.wait(), timeout=5)
    try:
        async with session_factory() as session:
            marker = await session.execute(
                text(
                    "SELECT commit_started_at FROM memory_operations "
                    "WHERE operation_id = :operation_id"
                ),
                {"operation_id": operation_id},
            )
        assert marker.scalar_one() is None  # 执行开始已清除残留标记

        response = await api_client.post(
            f"/api/v1/memory/operations/{operation_id}/cancel",
            headers={"Idempotency-Key": "it-k-stale-cancel", **_auth()},
        )
        assert response.status_code == 202  # 协作取消受理而非 409
    finally:
        runner.release.set()
        await asyncio.wait_for(task, timeout=10)

    async with session_factory() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT status, cancel_requested_at, commit_started_at "
                        "FROM memory_operations WHERE operation_id = :operation_id"
                    ),
                    {"operation_id": operation_id},
                )
            )
            .mappings()
            .one()
        )
    assert row["cancel_requested_at"] is not None
    assert row["commit_started_at"] is None  # 终态落库同时清除标记


async def test_account_purge_roundtrip(
    api_client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """§19.7：identity mapping 解析 → manifest + operation + Outbox 同事务。"""
    target_user = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await IdentityMappingRepository(session).create(
                internal_user_id=target_user,
                issuer="https://accounts.example",
                external_subject="ext-sub-1",
            )

    purge_body = {
        "account_deletion_id": str(uuid4()),
        "issuer": "https://accounts.example",
        "external_subject": "ext-sub-1",
        "requested_at": "2026-08-11T00:00:00Z",
        "reason": "用户注销",
    }
    service_headers = _auth(
        uuid4(), **{"X-Dev-Actor-Type": "system", "X-Dev-Scopes": "memory:maintenance"}
    )
    response = await api_client.post(
        "/api/v1/internal/account-memory/purge", json=purge_body, headers=service_headers
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["operation_type"] == "purge_account_memory"
    assert body["status"] == "queued"

    async with session_factory() as session:
        manifest = (
            (await session.execute(text("SELECT status, user_hash FROM account_deletion_manifest")))
            .mappings()
            .one()
        )
        assert manifest["status"] == "requested"
        assert str(target_user) not in manifest["user_hash"]  # 只存摘要（§13.16）
        outbox = (
            (await session.execute(text("SELECT event_type, payload FROM memory_outbox")))
            .mappings()
            .one()
        )
        assert outbox["event_type"] == "account_memory.purge_requested"
        operation = (
            (
                await session.execute(
                    text("SELECT user_id, actor_type, operation_type FROM memory_operations")
                )
            )
            .mappings()
            .one()
        )
        assert str(operation["user_id"]) == str(target_user)
        assert operation["actor_type"] == "system"

    # 幂等重放：同一 account_deletion_id 返回原 operation
    replay = await api_client.post(
        "/api/v1/internal/account-memory/purge", json=purge_body, headers=service_headers
    )
    assert replay.status_code == 202
    assert replay.json()["operation_id"] == body["operation_id"]

    # 同一用户不同 account_deletion_id → 409
    conflict = await api_client.post(
        "/api/v1/internal/account-memory/purge",
        json={**purge_body, "account_deletion_id": str(uuid4())},
        headers=service_headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "ACCOUNT_PURGE_ALREADY_RUNNING"

    # 未注册映射 → 404
    missing = await api_client.post(
        "/api/v1/internal/account-memory/purge",
        json={**purge_body, "account_deletion_id": str(uuid4()), "external_subject": "unknown-sub"},
        headers=service_headers,
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "IDENTITY_MAPPING_NOT_FOUND"


async def test_account_purge_blocks_new_operations(
    api_client: httpx.AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """§21.3 步骤 1（评审 P0-1）：manifest 存在时阻止该用户创建新 operation。"""
    target_user = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await IdentityMappingRepository(session).create(
                internal_user_id=target_user,
                issuer="https://accounts.example",
                external_subject="ext-blocked-1",
            )
    service_headers = _auth(
        uuid4(), **{"X-Dev-Actor-Type": "system", "X-Dev-Scopes": "memory:maintenance"}
    )
    purge_body = {
        "account_deletion_id": str(uuid4()),
        "issuer": "https://accounts.example",
        "external_subject": "ext-blocked-1",
        "requested_at": "2026-08-12T00:00:00Z",
        "reason": "用户注销",
    }
    created = await api_client.post(
        "/api/v1/internal/account-memory/purge", json=purge_body, headers=service_headers
    )
    assert created.status_code == 202

    blocked = await api_client.post(
        "/api/v1/memory/events",
        json=EVENT_BODY,
        headers={"Idempotency-Key": f"blocked-{uuid4().hex[:8]}", **_agent_auth(target_user)},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "ACCOUNT_PURGE_IN_PROGRESS"


async def test_account_purge_replay_after_completion(
    api_client: httpx.AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    runner: LocalLangGraphRunner,
) -> None:
    """§13.16 / §7.1（评审 P0-1）：purge 完成后自身 operation 行已物理删除，
    幂等重放应返回合成的 succeeded 终态结果而不是报错。"""
    target_user = uuid4()
    async with session_factory() as session:
        async with session.begin():
            await IdentityMappingRepository(session).create(
                internal_user_id=target_user,
                issuer="https://accounts.example",
                external_subject="ext-replay-1",
            )
    service_headers = _auth(
        uuid4(), **{"X-Dev-Actor-Type": "system", "X-Dev-Scopes": "memory:maintenance"}
    )
    purge_body = {
        "account_deletion_id": str(uuid4()),
        "issuer": "https://accounts.example",
        "external_subject": "ext-replay-1",
        "requested_at": "2026-08-12T00:00:00Z",
        "reason": "用户注销",
    }
    created = await api_client.post(
        "/api/v1/internal/account-memory/purge", json=purge_body, headers=service_headers
    )
    assert created.status_code == 202
    operation_id = UUID(created.json()["operation_id"])

    # Worker 执行 purge（完成后 operation 行被物理删除）
    async with session_factory() as session:
        row = await ops_repo.get_operation(session, operation_id)
    assert row is not None
    operation = MemoryOperation.model_validate(
        {k: row[k] for k in MemoryOperation.model_fields if k in row}
    )
    result = await runner.run(operation)
    assert result.status == "succeeded"
    async with session_factory() as session:
        gone = await ops_repo.get_operation(session, operation_id)
    assert gone is None  # 自身行已删除（§13.16）

    # 幂等重放：合成 succeeded 结果
    replay = await api_client.post(
        "/api/v1/internal/account-memory/purge", json=purge_body, headers=service_headers
    )
    assert replay.status_code == 200
    body = replay.json()
    assert body["operation_id"] == str(operation_id)
    assert body["status"] == "succeeded"
    assert body["operation_type"] == "purge_account_memory"
