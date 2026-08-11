"""API 单元测试共享 fake（§23.5）：内存 operation 仓储、Runner 与应用装配。

仓储函数通过 install(monkeypatch) 替换 backend.memory.persistence.operations
上的实现，会话使用 worker_fakes.FakeSessionFactory，不起真实 PostgreSQL。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import FastAPI

from backend.app import create_app
from backend.memory.api.dependencies import ApiRuntime
from backend.memory.contracts.errors import OperationCancelNotAllowedError
from backend.memory.contracts.operations import MemoryOperation, MemoryOperationResult
from backend.memory.persistence import operations as ops_repo
from backend.memory.worker.worker import Worker, WorkerConfig
from backend.settings import Settings
from tests.unit.worker_fakes import make_session_factory


class InMemoryOperationStore:
    """memory_operations 的内存实现，语义对齐 persistence/operations.py。"""

    def __init__(self) -> None:
        self.rows: dict[UUID, dict[str, Any]] = {}

    def _row_from_operation(self, operation: MemoryOperation, payload_hash: str) -> dict[str, Any]:
        now = datetime.now(UTC)
        return {
            "operation_id": operation.operation_id,
            "user_id": operation.user_id,
            "actor_type": operation.actor_type,
            "input_kind": operation.input_kind,
            "operation_type": operation.operation_type,
            "idempotency_key": operation.idempotency_key,
            "idempotency_payload_hash": payload_hash,
            "priority": operation.priority,
            "status": "queued",
            "payload": operation.payload.model_dump(mode="json"),
            "result": None,
            "public_error": None,
            "trace_id": operation.trace_id,
            "graph_thread_id": operation.graph_thread_id,
            "occurred_at": operation.occurred_at,
            "created_at": now,
            "updated_at": now,
            "completed_at": None,
            "next_run_at": now,
            "attempt_count": 0,
            "max_attempts": 6,
            "locked_by": None,
            "lease_expires_at": None,
            "cancel_requested_at": None,
            "commit_started_at": None,
        }

    def _find_by_key(self, user_id: UUID, actor_type: str, key: str) -> dict[str, Any] | None:
        for row in self.rows.values():
            if (
                row["user_id"] == user_id
                and row["actor_type"] == actor_type
                and row["idempotency_key"] == key
            ):
                return row
        return None

    async def insert_operation(
        self, session: Any, operation: MemoryOperation, *, idempotency_payload_hash: str
    ) -> bool:
        if (
            self._find_by_key(operation.user_id, operation.actor_type, operation.idempotency_key)
            is not None
        ):
            return False
        self.rows[operation.operation_id] = self._row_from_operation(
            operation, idempotency_payload_hash
        )
        return True

    async def get_by_idempotency(
        self, session: Any, *, user_id: UUID, actor_type: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        return self._find_by_key(user_id, actor_type, idempotency_key)

    async def get_operation(self, session: Any, operation_id: UUID) -> dict[str, Any] | None:
        return self.rows.get(operation_id)

    async def list_user_operations(
        self, session: Any, *, user_id: UUID, operation_id: UUID
    ) -> dict[str, Any] | None:
        row = self.rows.get(operation_id)
        if row is None or row["user_id"] != user_id:
            return None
        return row

    async def claim_operation(
        self,
        session: Any,
        *,
        worker_id: str,
        lease_seconds: int,
        operation_id: UUID | None = None,
        batch_size: int = 10,
    ) -> list[dict[str, Any]]:
        candidates = [
            row for row in self.rows.values() if row["status"] in ("queued", "retry_wait")
        ]
        if operation_id is not None:
            candidates = [row for row in candidates if row["operation_id"] == operation_id]
        claimed: list[dict[str, Any]] = []
        for row in candidates[:batch_size]:
            row["status"] = "running"
            row["locked_by"] = worker_id
            row["attempt_count"] += 1
            row["updated_at"] = datetime.now(UTC)
            claimed.append(row)
        return claimed

    async def request_cancel(self, session: Any, *, operation_id: UUID) -> dict[str, Any] | None:
        row = self.rows.get(operation_id)
        if row is None:
            return None
        status = row["status"]
        if status in ("queued", "retry_wait", "needs_review"):
            row["status"] = "cancelled"
            row["completed_at"] = datetime.now(UTC)
        elif status == "running":
            if row["commit_started_at"] is not None:
                raise OperationCancelNotAllowedError(
                    "operation 已进入 commit，不允许取消", field="status"
                )
            row["cancel_requested_at"] = datetime.now(UTC)
        else:
            return None
        row["updated_at"] = datetime.now(UTC)
        return row

    async def mark_commit_started(self, session: Any, *, operation_id: UUID) -> None:
        row = self.rows.get(operation_id)
        if row is not None and row["status"] == "running":
            row["commit_started_at"] = datetime.now(UTC)

    async def clear_commit_started(self, session: Any, *, operation_id: UUID) -> None:
        row = self.rows.get(operation_id)
        if row is not None:
            row["commit_started_at"] = None

    async def get_cancel_requested(self, session: Any, operation_id: UUID) -> bool:
        row = self.rows.get(operation_id)
        return bool(row and row["cancel_requested_at"])

    async def complete_operation(
        self,
        session: Any,
        *,
        operation_id: UUID,
        status: str,
        result: dict[str, Any] | None,
        public_error: dict[str, Any] | None,
        llm_call_count: int = 0,
    ) -> None:
        row = self.rows[operation_id]
        row["status"] = status
        row["result"] = result
        row["public_error"] = public_error
        row["completed_at"] = datetime.now(UTC)
        row["updated_at"] = datetime.now(UTC)
        row["commit_started_at"] = None

    async def heartbeat(
        self, session: Any, *, operation_id: UUID, worker_id: str, lease_seconds: int
    ) -> bool:
        return True

    async def reschedule_operation(
        self, session: Any, *, operation_id: UUID, next_run_at: datetime, status: str
    ) -> None:
        self.rows[operation_id]["status"] = status
        self.rows[operation_id]["commit_started_at"] = None

    def install(self, monkeypatch: Any) -> None:
        for name in (
            "insert_operation",
            "get_by_idempotency",
            "get_operation",
            "list_user_operations",
            "claim_operation",
            "request_cancel",
            "get_cancel_requested",
            "complete_operation",
            "heartbeat",
            "reschedule_operation",
            "mark_commit_started",
            "clear_commit_started",
        ):
            monkeypatch.setattr(ops_repo, name, getattr(self, name))


class FakeRunner:
    """可配置延迟/结果的 MemoryGraphRunner。"""

    def __init__(self, *, delay: float = 0.0, result_status: str = "succeeded") -> None:
        self.delay = delay
        self.result_status = result_status
        self.calls: list[MemoryOperation] = []

    async def run(self, operation: MemoryOperation) -> MemoryOperationResult:
        self.calls.append(operation)
        if self.delay:
            await asyncio.sleep(self.delay)
        now = datetime.now(UTC)
        return MemoryOperationResult(
            operation_id=operation.operation_id,
            status=self.result_status,  # type: ignore[arg-type]
            operation_type=operation.operation_type,
            created_at=operation.occurred_at,
            updated_at=now,
            completed_at=now,
        )


class FakeMemoryService:
    """读接口替身：learner/mastery/index 由测试直接赋值。"""

    def __init__(self) -> None:
        self.learner: Any = None
        self.mastery: Any = None
        self.index: tuple[Any, bool] = (None, True)

    async def get_learner(self, *, user_id: UUID) -> Any:
        return self.learner

    async def get_mastery(self, *, user_id: UUID, topic_key: str) -> Any:
        return self.mastery

    async def get_index(self, *, user_id: UUID) -> tuple[Any, bool]:
        return self.index


def build_test_app(
    settings: Settings,
    *,
    monkeypatch: Any,
    store: InMemoryOperationStore | None = None,
    runner: FakeRunner | None = None,
    memory_service: FakeMemoryService | None = None,
) -> tuple[FastAPI, InMemoryOperationStore, FakeRunner, FakeMemoryService]:
    """装配 TestClient 可用的 app：FakeSessionFactory + 内存仓储 + fake runner。"""
    store = store or InMemoryOperationStore()
    store.install(monkeypatch)
    runner = runner or FakeRunner()
    memory_service = memory_service or FakeMemoryService()
    session_factory = make_session_factory()
    gateway_worker = Worker(
        session_factory=session_factory,
        runner=runner,
        config=WorkerConfig(),
        worker_id="gateway-test",
    )
    runtime = ApiRuntime(
        settings=settings,
        session_factory=session_factory,
        memory_service=memory_service,  # type: ignore[arg-type]
        runner=runner,
        gateway_worker=gateway_worker,
    )
    app = create_app(settings, runtime=runtime)
    return app, store, runner, memory_service
