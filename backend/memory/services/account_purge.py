"""账号物理删除核心流程（§13.16 / §21.3，评审 P0-1/P0-2 修复）。

purge_user_account 被两处复用：
1. Maintenance 分支的 purge_account_memory operation（Worker 执行）；
2. 备份恢复流程的账号删除重放（restore 时同步执行，完成前不返回）。

分三个阶段，阶段间崩溃可安全重试（全部操作幂等）：
- 阶段 1（单事务）：收集 Checkpoint 线程 → break-glass 压缩 → 逐表物理删除
  （purge 自身 operation 行保留到阶段 3，保证崩溃后执行层仍能重试）；
- 阶段 2（无事务）：Markdown 用户树与 LangGraph Checkpoint 删除；
- 阶段 3（单事务）：删除 purge 自身 operation/run 行 → 不可还原完成证明
  → manifest/ledger 推进 → 完成审计。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.contracts.common import canonical_json, user_privacy_hash
from backend.memory.contracts.errors import MemoryError
from backend.memory.persistence import account_deletion as deletion_repo
from backend.memory.persistence.database import exec_rowcount
from backend.settings import Settings

_BACKUP_RETENTION = timedelta(days=30)


class AccountPurgeNotDrainedError(MemoryError):
    """用户仍有运行中的 operation 未终止；purge 操作可重试等待。"""

    code = "ACCOUNT_PURGE_NOT_DRAINED"
    retryable = True


@dataclass
class PurgeSummary:
    """单次 purge 的计数结果（只含计数与证明，不含用户正文）。"""

    user_hash: str
    table_counts: dict[str, int] = field(default_factory=dict)
    checkpoint_threads_deleted: int = 0
    markdown_tree_deleted: bool = False
    break_glass_compressed: int = 0
    completion_proof_checksum: str = ""


def _proof_checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


async def drain_user_operations(
    session: AsyncSession, *, user_id: UUID, exclude_operation_id: UUID
) -> int:
    """取消未开始的用户 operation 并请求终止运行中的（§21.3 步骤 2-3）。

    返回仍在运行的 operation 数（不含 purge 自身）；大于 0 时调用方应以
    AccountPurgeNotDrainedError 触发退避重试，等待其退出。
    """
    from backend.memory.contracts.errors import OperationCancelNotAllowedError
    from backend.memory.persistence import operations as ops_repo

    rows = await session.execute(
        text(
            "SELECT operation_id, status FROM memory_operations "
            "WHERE user_id = :user_id AND operation_id != :exclude "
            "AND status IN ('queued', 'retry_wait', 'needs_review', 'running')"
        ),
        {"user_id": user_id, "exclude": exclude_operation_id},
    )
    running = 0
    for row in rows.mappings().all():
        if row["status"] == "running":
            try:
                await ops_repo.request_cancel(session, operation_id=row["operation_id"])
            except OperationCancelNotAllowedError:
                pass  # 已进入 commit：不允许取消，等待其自然结束
            running += 1
        else:
            await ops_repo.request_cancel(session, operation_id=row["operation_id"])
    return running


async def purge_user_account(
    session_factory: Any,
    *,
    settings: Settings,
    store: Any,
    checkpoint_cleanup: Any | None,
    user_id: UUID,
    account_deletion_id: UUID | None,
    now: datetime,
    requested_at: datetime | None = None,
    self_operation_id: UUID | None = None,
) -> PurgeSummary:
    """执行账号物理删除全流程。所有阶段幂等，可整体重试。

    account_deletion_id 为 None 时按 user_hash 查 manifest 取得（正常路径）；
    恢复重放路径传入 ledger/manifest 条目中的 id 与 requested_at
    （目标库可能尚无 manifest 行，补齐时保留原始时间）。
    self_operation_id 为 purge 自身 operation 时，其行保留到阶段 3 才删除，
    避免阶段 1/2 之间崩溃后执行层丢失重试锚点。
    """
    user_hash = user_privacy_hash(settings.privacy_hmac_key, str(user_id))
    key_version = settings.privacy_hmac_key_version
    summary = PurgeSummary(user_hash=user_hash)

    async with session_factory() as session:
        manifest = await deletion_repo.get_manifest_by_user_hash(session, user_hash=user_hash)
    if manifest is None and account_deletion_id is None:
        raise LookupError(f"账号删除 manifest 不存在: user_hash={user_hash[:12]}…")
    if account_deletion_id is None:
        assert manifest is not None
        account_deletion_id = UUID(str(manifest["account_deletion_id"]))
    requested_at_value: datetime = (
        manifest["requested_at"] if manifest is not None else (requested_at or now)
    )

    # ------------------------------------------------------------------
    # 阶段 1（单事务）：收集 Checkpoint 线程 → break-glass 压缩 → 逐表删除
    # ------------------------------------------------------------------
    thread_ids: list[str] = []
    async with session_factory() as session:
        async with session.begin():
            await deletion_repo.ensure_ops_schema(session)
            thread_ids = await _checkpoint_thread_ids(session, user_id=user_id)
            if manifest is None:
                # 恢复重放时备份内可能没有该 manifest（ledger 独有条目）：补齐
                await deletion_repo.insert_manifest(
                    session,
                    account_deletion_id=account_deletion_id,
                    user_hash=user_hash,
                    user_hash_key_version=key_version,
                    requested_at=requested_at_value,
                    backup_retention_until=requested_at_value + _BACKUP_RETENTION,
                )
            await deletion_repo.update_manifest_status(
                session, account_deletion_id=account_deletion_id, status="running"
            )
            await deletion_repo.upsert_ledger_entry(
                session,
                account_deletion_id=account_deletion_id,
                user_hash=user_hash,
                user_hash_key_version=key_version,
                status="running",
                requested_at=requested_at_value,
            )
            await _compress_break_glass(
                session,
                settings=settings,
                user_id=user_id,
                user_hash=user_hash,
                key_version=key_version,
                now=now,
                summary=summary,
            )
            await _delete_user_rows(
                session,
                user_id=user_id,
                user_hash=user_hash,
                summary=summary,
                exclude_operation_id=self_operation_id,
            )

    # ------------------------------------------------------------------
    # 阶段 2（无事务）：Markdown 用户树 + Checkpoint（阶段 1 收集的线程）
    # ------------------------------------------------------------------
    await store.delete_user_tree(user_id=user_id)
    summary.markdown_tree_deleted = True
    if checkpoint_cleanup is not None and thread_ids:
        summary.checkpoint_threads_deleted = await checkpoint_cleanup.delete_threads(thread_ids)

    # ------------------------------------------------------------------
    # 阶段 3（单事务）：完成证明 + manifest/ledger 推进 + 完成审计
    # ------------------------------------------------------------------
    completed_at = now
    proof = _proof_checksum(
        {
            "account_deletion_id": str(account_deletion_id),
            "user_hash": user_hash,
            "purge_completed_at": completed_at.isoformat(),
            "table_counts": summary.table_counts,
            "checkpoint_threads_deleted": summary.checkpoint_threads_deleted,
            "markdown_tree_deleted": summary.markdown_tree_deleted,
            "break_glass_compressed": summary.break_glass_compressed,
        }
    )
    summary.completion_proof_checksum = proof
    async with session_factory() as session:
        async with session.begin():
            if self_operation_id is not None:
                # 收尾：删除 purge 自身 run/operation 行（引用方已在阶段 1 清空）
                await exec_rowcount(
                    session,
                    text("DELETE FROM memory_maintenance_runs WHERE operation_id = :operation_id"),
                    {"operation_id": self_operation_id},
                )
                await exec_rowcount(
                    session,
                    text("DELETE FROM memory_operations WHERE operation_id = :operation_id"),
                    {"operation_id": self_operation_id},
                )
            await deletion_repo.update_manifest_status(
                session,
                account_deletion_id=account_deletion_id,
                status="completed",
                purge_completed_at=completed_at,
                completion_proof_checksum=proof,
            )
            await deletion_repo.upsert_ledger_entry(
                session,
                account_deletion_id=account_deletion_id,
                user_hash=user_hash,
                user_hash_key_version=key_version,
                status="completed",
                requested_at=requested_at_value,
                purge_completed_at=completed_at,
                completion_proof_checksum=proof,
            )
            await deletion_repo.insert_privacy_audit(
                session,
                privacy_audit_id=uuid4(),
                user_hash=user_hash,
                user_hash_key_version=key_version,
                action="account_memory_purged",
                actor_hash=None,
                occurred_at=completed_at,
                proof_checksum=proof,
            )
    return summary


async def _checkpoint_thread_ids(session: AsyncSession, *, user_id: UUID) -> list[str]:
    """阶段 1 内收集该用户全部 operation 的 checkpoint thread（§21.3 步骤 6）。

    必须在 memory_operations 删除前调用；恢复重放等 operation 行已不存在的
    场景返回空列表（其 Checkpoint 已随 public schema 重置清除）。
    """
    rows = await session.execute(
        text("SELECT operation_id FROM memory_operations WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    from backend.memory.worker.checkpoint import thread_id_for_operation

    return [thread_id_for_operation(row.operation_id) for row in rows.all()]


async def _compress_break_glass(
    session: AsyncSession,
    *,
    settings: Settings,
    user_id: UUID,
    user_hash: str,
    key_version: str,
    now: datetime,
    summary: PurgeSummary,
) -> None:
    """break-glass 授权/审计压缩为最小隐私审计后删除原始记录（§13.16）。"""
    audits = await session.execute(
        text(
            "SELECT audit_id, grant_id, admin_user_id, action, resource_type, created_at "
            "FROM memory_break_glass_audit WHERE target_user_id = :user_id"
        ),
        {"user_id": user_id},
    )
    for row in audits.mappings().all():
        actor_hash = user_privacy_hash(settings.privacy_hmac_key, str(row["admin_user_id"]))
        proof = _proof_checksum(
            {
                "audit_id": str(row["audit_id"]),
                "grant_id": str(row["grant_id"]),
                "action": row["action"],
                "created_at": row["created_at"].isoformat(),
            }
        )
        await deletion_repo.insert_privacy_audit(
            session,
            privacy_audit_id=uuid4(),
            user_hash=user_hash,
            user_hash_key_version=key_version,
            action=f"break_glass.{row['action']}",
            actor_hash=actor_hash,
            occurred_at=row["created_at"],
            proof_checksum=proof,
        )
        summary.break_glass_compressed += 1
    grants = await session.execute(
        text(
            "SELECT grant_id, admin_user_id, approved_by, created_at "
            "FROM memory_break_glass_grants WHERE target_user_id = :user_id"
        ),
        {"user_id": user_id},
    )
    for row in grants.mappings().all():
        actor_hash = user_privacy_hash(settings.privacy_hmac_key, str(row["admin_user_id"]))
        proof = _proof_checksum(
            {
                "grant_id": str(row["grant_id"]),
                "created_at": row["created_at"].isoformat(),
            }
        )
        await deletion_repo.insert_privacy_audit(
            session,
            privacy_audit_id=uuid4(),
            user_hash=user_hash,
            user_hash_key_version=key_version,
            action="break_glass.grant",
            actor_hash=actor_hash,
            occurred_at=row["created_at"],
            proof_checksum=proof,
        )
        summary.break_glass_compressed += 1
    await exec_rowcount(
        session,
        text("DELETE FROM memory_break_glass_audit WHERE target_user_id = :user_id"),
        {"user_id": user_id},
    )
    await exec_rowcount(
        session,
        text("DELETE FROM memory_break_glass_grants WHERE target_user_id = :user_id"),
        {"user_id": user_id},
    )


#: 逐表删除顺序（FK 安全）：先删引用方，最后删 account_identity_mappings。
#: (表名, 匹配列)；user_hash 列按摘要匹配，其余按 user_id 匹配；
#: memory_outbox_deliveries / memory_maintenance_runs / memory_operations 有特殊逻辑。
_USER_TABLE_DELETES: tuple[tuple[str, str], ...] = (
    ("memory_user_notifications", "user_id"),
    ("memory_internal_event_log", "user_id"),
    ("memory_outbox_deliveries", ""),
    ("memory_outbox", "user_id"),
    ("memory_llm_call_metrics", "user_hash"),
    ("graph_state_audit", "user_id"),
    ("graph_user_node_activity", "user_id"),
    ("graph_activity_seen_events", "user_id"),
    ("graph_user_states", "user_id"),
    ("source_deletions", "user_id"),
    ("memory_graph_links", "user_id"),
    ("memory_deleted_evidence_suppressions", "user_id"),
    ("memory_review_candidates", "user_id"),
    ("memory_index_entries", "user_id"),
    ("memory_commits", "user_id"),
    ("memory_documents", "user_id"),
    ("memory_maintenance_runs", ""),
    ("memory_operations", ""),
    ("account_identity_mappings", "internal_user_id"),
)


async def _delete_user_rows(
    session: AsyncSession,
    *,
    user_id: UUID,
    user_hash: str,
    summary: PurgeSummary,
    exclude_operation_id: UUID | None,
) -> None:
    """按 §13.16 逐表规则物理删除用户数据（FK 安全顺序，幂等）。

    exclude_operation_id（purge 自身）的 memory_operations/maintenance_runs 行
    在阶段 3 才删除，保证阶段间崩溃后执行层仍能按原 operation 重试。
    """
    for table, column in _USER_TABLE_DELETES:
        if table == "memory_outbox_deliveries":
            # deliveries 无 user_id 列，经 memory_outbox ON DELETE CASCADE 清理；
            # 这里显式按用户 outbox 子查询删除，兼容无 cascade 的旧快照。
            count = await exec_rowcount(
                session,
                text(
                    "DELETE FROM memory_outbox_deliveries WHERE outbox_id IN "
                    "(SELECT outbox_id FROM memory_outbox WHERE user_id = :user_id)"
                ),
                {"user_id": user_id},
            )
        elif table == "memory_maintenance_runs":
            count = await exec_rowcount(
                session,
                text(
                    "DELETE FROM memory_maintenance_runs WHERE operation_id IN "
                    "(SELECT operation_id FROM memory_operations "
                    "WHERE user_id = :user_id AND operation_id != :exclude)"
                ),
                {"user_id": user_id, "exclude": exclude_operation_id or UUID(int=0)},
            )
        elif table == "memory_operations":
            count = await exec_rowcount(
                session,
                text(
                    "DELETE FROM memory_operations "
                    "WHERE user_id = :user_id AND operation_id != :exclude"
                ),
                {"user_id": user_id, "exclude": exclude_operation_id or UUID(int=0)},
            )
        elif column == "user_hash":
            count = await exec_rowcount(
                session,
                text(f"DELETE FROM {table} WHERE {column} = :user_hash"),
                {"user_hash": user_hash},
            )
        else:
            count = await exec_rowcount(
                session,
                text(f"DELETE FROM {table} WHERE {column} = :user_id"),
                {"user_id": user_id},
            )
        summary.table_counts[table] = count
