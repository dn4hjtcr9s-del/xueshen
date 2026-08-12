"""Maintenance 分支（§10.7）：有界 batch + cursor，不长时间持锁。

步骤 9 实现 rebuild_index / purge_tombstones / cleanup_orphan_versions；
步骤 10 接入 cleanup_checkpoints（CheckpointCleanupAdapter，§11.4）；
purge_account_memory 走 account_purge 服务（§13.16/§21.3，评审 P0-1 修复）；
verify_checksums（§14.3，每天 04:00）：校验活动版本 checksum 与解析合法性、
current/ 物化副本与活动版本一致性（漂移时重新物化），损坏项告警并记入 run detail。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.runtime import Runtime
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.contracts.commands import MaintenanceCommand
from backend.memory.contracts.errors import InvalidPayloadError
from backend.memory.contracts.operations import MemoryOperation
from backend.memory.graph.state import MemoryManagerState, MemoryRuntimeContext
from backend.memory.persistence import documents as docs_repo
from backend.memory.persistence import maintenance as maintenance_repo
from backend.memory.persistence.database import exec_rowcount
from backend.memory.storage.base import sha256_hex
from backend.memory.storage.markdown_schema import MarkdownParseError, parse_learner, parse_mastery

logger = logging.getLogger("memory.maintenance")


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

    # acquire_scheduler_lock：每类维护任务全局互斥（§14.3）
    async with ctx.session_factory() as session:
        result = await session.execute(
            text("SELECT pg_try_advisory_lock(hashtext(:name))"),
            {"name": f"maintenance:{kind}"},
        )
        acquired = bool(result.scalar_one())
        # 结束 autobegin 事务；session 级 advisory lock 不受 commit 影响
        await session.commit()
        if not acquired:
            detail = {"kind": kind, "status": "busy"}
            # busy 也回写 run（保持 running），由 Scheduler 稍后重排同 cursor 批次
            async with session.begin():
                await maintenance_repo.update_run_by_operation(
                    session,
                    operation_id=operation.operation_id,
                    status="running",
                    cursor=payload.cursor,
                    result=detail,
                )
            return {
                "graph_state_result": {"maintenance": detail},
                "warnings": [*state.get("warnings", []), f"维护任务 {kind} 正在其他实例运行"],
            }
        try:
            async with session.begin():
                detail = await _execute_batch(ctx, operation, payload, session)
                # persist_cursor_or_finish：回写 maintenance run（§10.7 / §14.3）
                await maintenance_repo.update_run_by_operation(
                    session,
                    operation_id=operation.operation_id,
                    status="running" if detail.get("status") == "continue" else "succeeded",
                    cursor=detail.get("next_cursor"),
                    result=detail,
                )
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

    if kind == "cleanup_checkpoints":
        from backend.memory.worker.checkpoint import (
            list_expired_checkpoint_threads,
            list_orphan_checkpoint_threads,
        )

        if ctx.checkpoint_cleanup is None:
            raise InvalidPayloadError(
                "cleanup_checkpoints 需要 Runtime Context 配置 CheckpointCleanupAdapter"
            )
        rows = await list_expired_checkpoint_threads(
            session, now=now, batch_size=payload.batch_size, cursor=payload.cursor
        )
        deleted = 0
        next_cursor = None
        for row in rows:
            next_cursor = row["cursor"]
            if payload.dry_run:
                continue
            deleted += await ctx.checkpoint_cleanup.delete_threads([row["thread_id"]])
        finished = len(rows) < payload.batch_size
        # 到期扫描完成后，用剩余配额清扫孤儿线程（账号删除自身线程等遗留）
        orphans_deleted = 0
        if finished and not payload.dry_run:
            orphans = await list_orphan_checkpoint_threads(
                session, batch_size=payload.batch_size - len(rows)
            )
            for thread_id in orphans:
                orphans_deleted += await ctx.checkpoint_cleanup.delete_threads([thread_id])
        return {
            "kind": kind,
            "status": "done" if finished else "continue",
            "scanned": len(rows),
            "threads_deleted": deleted,
            "orphan_threads_deleted": orphans_deleted,
            "dry_run": payload.dry_run,
            "next_cursor": None if finished else next_cursor,
        }

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

    if kind == "purge_account_memory":
        # §21.3 / §13.16：账号物理删除全流程（评审 P0-1 修复）。
        # 注意：purge 会删除自身的 memory_operations/memory_maintenance_runs 行，
        # 外层 update_run_by_operation 与 complete_operation 均为静默 no-op。
        purge_target = payload.target_user_id
        if purge_target is None:
            raise InvalidPayloadError("purge_account_memory 需要 target_user_id")
        from backend.memory.services.account_purge import (
            AccountPurgeNotDrainedError,
            drain_user_operations,
            purge_user_account,
        )

        async with ctx.session_factory() as drain_session:
            async with drain_session.begin():
                running = await drain_user_operations(
                    drain_session,
                    user_id=purge_target,
                    exclude_operation_id=operation.operation_id,
                )
        if running > 0:
            # 可重试错误：执行层按退避重排，等待运行中任务退出（§21.3 步骤 3）
            raise AccountPurgeNotDrainedError(f"账号删除等待 {running} 个运行中用户任务结束")
        if payload.dry_run:
            return {"kind": kind, "status": "done", "dry_run": True}
        summary = await purge_user_account(
            ctx.session_factory,
            settings=ctx.settings,
            store=ctx.memory_service.store,
            checkpoint_cleanup=ctx.checkpoint_cleanup,
            user_id=purge_target,
            account_deletion_id=None,
            now=now,
            self_operation_id=operation.operation_id,
        )
        return {
            "kind": kind,
            "status": "done",
            "purged_tables": summary.table_counts,
            "checkpoint_threads_deleted": summary.checkpoint_threads_deleted,
            "markdown_tree_deleted": summary.markdown_tree_deleted,
            "break_glass_compressed": summary.break_glass_compressed,
            "completion_proof_checksum": summary.completion_proof_checksum,
        }

    if kind == "verify_checksums":
        # §14.3：校验活动版本 checksum/解析合法性，以及 current/ 物化副本一致性；
        # 漂移副本按活动版本重新物化修复，损坏项告警并记入 detail。
        rows = await docs_repo.list_active_documents_page(
            session, batch_size=payload.batch_size, cursor=payload.cursor
        )
        checked = 0
        rematerialized = 0
        corrupted: list[dict[str, Any]] = []
        next_cursor = None
        for row in rows:
            next_cursor = f"{row['user_id']}:{row['memory_id']}"
            checked += 1
            issue: dict[str, Any] = {
                "user_id": str(row["user_id"]),
                "memory_id": row["memory_id"],
                "reasons": [],
            }
            try:
                content = await store.read_version(
                    user_id=row["user_id"], storage_key=row["active_storage_key"]
                )
            except FileNotFoundError:
                issue["reasons"].append("active_version_missing")
                content = None
            trusted = content is not None
            if content is not None:
                if sha256_hex(content) != row["active_checksum"]:
                    issue["reasons"].append("checksum_mismatch")
                    trusted = False
                if row["memory_type"] == "learner":
                    try:
                        parse_learner(content.decode("utf-8"))
                    except (MarkdownParseError, UnicodeDecodeError):
                        issue["reasons"].append("parse_failed")
                elif row["memory_type"] == "mastery":
                    try:
                        parse_mastery(content.decode("utf-8"))
                    except (MarkdownParseError, UnicodeDecodeError):
                        issue["reasons"].append("parse_failed")
            drift = False
            try:
                current = await store.read_current(
                    user_id=row["user_id"], memory_id=row["memory_id"]
                )
                if content is not None and current != content:
                    drift = True
            except FileNotFoundError:
                drift = True
            if drift and content is not None:
                issue["reasons"].append("current_drift")
                # 仅在校验和可信时重新物化，避免把损坏内容传播到 current/
                if trusted and not payload.dry_run:
                    await store.materialize_current(
                        user_id=row["user_id"], memory_id=row["memory_id"], content=content
                    )
                    rematerialized += 1
            if issue["reasons"]:
                corrupted.append(issue)
                logger.error(
                    "告警：verify_checksums 发现损坏 user_id=%s memory_id=%s reasons=%s",
                    row["user_id"],
                    row["memory_id"],
                    ",".join(issue["reasons"]),
                )
        finished = len(rows) < payload.batch_size
        return {
            "kind": kind,
            "status": "done" if finished else "continue",
            "checked": checked,
            "corrupted": corrupted,
            "rematerialized": rematerialized,
            "dry_run": payload.dry_run,
            "next_cursor": None if finished else next_cursor,
        }

    raise InvalidPayloadError(f"未知维护类型: {kind}")  # pragma: no cover
