"""Maintenance 分支（§10.7）：有界 batch + cursor，不长时间持锁。

步骤 9 实现 rebuild_index / purge_tombstones / cleanup_orphan_versions；
verify_checksums（步骤 15）、cleanup_checkpoints（步骤 10）、
purge_account_memory（步骤 15）在对应步骤接入，此处明确拒绝而非空转。
"""

from __future__ import annotations

from typing import Any

from langgraph.runtime import Runtime
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.contracts.commands import MaintenanceCommand
from backend.memory.contracts.errors import InvalidPayloadError
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext
from backend.memory.persistence import documents as docs_repo
from backend.memory.persistence.database import exec_rowcount

_NOT_IMPLEMENTED = {"verify_checksums", "cleanup_checkpoints", "purge_account_memory"}


async def run_maintenance(
    state: MemoryManagerState, runtime: Runtime[MemoryRuntimeContext]
) -> dict[str, Any]:
    """validate_maintenance_command → acquire_scheduler_lock → execute_bounded_batch
    → persist_cursor_or_finish → emit_maintenance_metric。"""
    ctx = runtime.context
    operation = MemoryOperation.model_validate(state["operation"])
    payload = operation.payload
    assert isinstance(payload, MaintenanceCommand)
    kind = payload.kind
    if kind in _NOT_IMPLEMENTED:
        raise InvalidPayloadError(f"维护类型 {kind} 尚未实现（对应后续步骤接入）")

    # acquire_scheduler_lock：每类维护任务全局互斥（§14.3）
    async with ctx.session_factory() as session:
        result = await session.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:name))"),
            {"name": f"maintenance:{kind}"},
        )
        acquired = bool(result.scalar_one())
        if not acquired:
            return {
                "graph_state_result": {"maintenance": {"kind": kind, "status": "busy"}},
                "warnings": [*state.get("warnings", []), f"维护任务 {kind} 正在其他实例运行"],
            }
        try:
            async with session.begin():
                detail = await _execute_batch(ctx, operation, payload, session)
        finally:
            await session.execute(
                text("SELECT pg_advisory_unlock(hashtext(:name))"),
                {"name": f"maintenance:{kind}"},
            )
    return {"graph_state_result": {"maintenance": detail}}


async def _execute_batch(
    ctx: MemoryRuntimeContext,
    operation: MemoryOperation,
    payload: MaintenanceCommand,
    session: AsyncSession,
) -> dict[str, Any]:
    kind = payload.kind
    now = ctx.clock.now()
    store = ctx.memory_service.store
    if kind == "rebuild_index":
        target = payload.target_user_id or operation.user_id
        result = await ctx.memory_service.rebuild_index(
            user_id=target, operation_id=operation.operation_id
        )
        return {"kind": kind, "status": "done", "result": result}

    if kind == "purge_tombstones":
        rows = await docs_repo.list_expired_tombstones(
            session, now=now, batch_size=payload.batch_size, cursor=payload.cursor
        )
        purged = 0
        next_cursor = None
        for row in rows:
            next_cursor = f"{row['user_id']}:{row['memory_id']}"
            if payload.dry_run:
                continue
            await store.purge_quarantined(user_id=row["user_id"], memory_id=row["memory_id"])
            orphans = await store.list_orphan_versions(
                user_id=row["user_id"],
                memory_id=row["memory_id"],
                referenced_checksums=set(),
            )
            for key in orphans:
                await store.delete_version_file(user_id=row["user_id"], storage_key=key)
            await exec_rowcount(
                session,
                text(
                    "DELETE FROM memory_documents "
                    "WHERE user_id = :u AND memory_id = :m AND deleted_at IS NOT NULL"
                ),
                {"u": row["user_id"], "m": row["memory_id"]},
            )
            purged += 1
        finished = len(rows) < payload.batch_size
        return {
            "kind": kind,
            "status": "done" if finished else "continue",
            "scanned": len(rows),
            "purged": purged,
            "dry_run": payload.dry_run,
            "next_cursor": None if finished else next_cursor,
        }

    if kind == "cleanup_orphan_versions":
        if payload.target_user_id is None:
            raise InvalidPayloadError("cleanup_orphan_versions 需要 target_user_id")
        target = payload.target_user_id
        docs = await docs_repo.list_active_documents(session, user_id=target)
        from sqlalchemy import text as sql_text

        removed = 0
        for doc in docs[: payload.batch_size]:
            checksum_rows = await session.execute(
                sql_text(
                    "SELECT checksum FROM memory_commits "
                    "WHERE user_id = :u AND memory_id = :m AND checksum IS NOT NULL"
                ),
                {"u": target, "m": doc["memory_id"]},
            )
            referenced = {str(r[0]) for r in checksum_rows.all()}
            orphans = await store.list_orphan_versions(
                user_id=target,
                memory_id=doc["memory_id"],
                referenced_checksums=referenced,
            )
            for key in orphans:
                if not payload.dry_run:
                    await store.delete_version_file(user_id=target, storage_key=key)
                removed += 1
        return {
            "kind": kind,
            "status": "done",
            "documents_scanned": min(len(docs), payload.batch_size),
            "orphans_removed": removed,
            "dry_run": payload.dry_run,
        }

    raise InvalidPayloadError(f"未知维护类型: {kind}")  # pragma: no cover
