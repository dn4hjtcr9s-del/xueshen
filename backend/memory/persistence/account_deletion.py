"""account_deletion_manifest 与 ops.account_deletion_ledger 仓储（§13.16 / §19.7 / §21.4）。

manifest 只保存 user_hash（privacy-audit 域摘要），不包含可还原的用户正文；
user_hash UNIQUE 保证同一用户最多一条 manifest。

ops.account_deletion_ledger 是账号删除的独立持久层（评审 P0-2 修复）：
恢复流程只重置 public schema，ops schema 不被 DROP，ledger 因此在同环境
恢复中存活；备份 manifest 内嵌 ledger 快照，支撑全新环境的灾难恢复重放。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount

#: ops schema 与 ledger 的幂等 DDL：恢复流程在 pg_restore 后确保其存在
#:（旧备份的 alembic 版本可能尚未包含 0004 迁移）。
OPS_ENSURE_DDL: tuple[str, ...] = (
    "CREATE SCHEMA IF NOT EXISTS ops",
    """
    CREATE TABLE IF NOT EXISTS ops.account_deletion_ledger (
        account_deletion_id uuid PRIMARY KEY,
        user_hash char(64) NOT NULL,
        user_hash_key_version varchar(32) NOT NULL,
        status text NOT NULL CHECK (status IN ('requested', 'running', 'completed', 'failed')),
        requested_at timestamptz NOT NULL,
        purge_completed_at timestamptz,
        completion_proof_checksum char(64),
        updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
)


async def insert_manifest(
    session: AsyncSession,
    *,
    account_deletion_id: UUID,
    user_hash: str,
    user_hash_key_version: str,
    requested_at: datetime,
    backup_retention_until: datetime,
) -> bool:
    """创建 manifest；user_hash 冲突（同一用户已有 manifest）返回 False。"""
    rowcount = await exec_rowcount(
        session,
        text(
            """
            INSERT INTO account_deletion_manifest (
                account_deletion_id, user_hash, user_hash_key_version,
                status, requested_at, backup_retention_until
            ) VALUES (
                :account_deletion_id, :user_hash, :user_hash_key_version,
                'requested', :requested_at, :backup_retention_until
            )
            ON CONFLICT (user_hash) DO NOTHING
            """
        ),
        {
            "account_deletion_id": account_deletion_id,
            "user_hash": user_hash,
            "user_hash_key_version": user_hash_key_version,
            "requested_at": requested_at,
            "backup_retention_until": backup_retention_until,
        },
    )
    return rowcount == 1


async def get_manifest_by_user_hash(
    session: AsyncSession, *, user_hash: str
) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM account_deletion_manifest WHERE user_hash = :user_hash"),
        {"user_hash": user_hash},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def get_manifest_by_id(
    session: AsyncSession, *, account_deletion_id: UUID
) -> dict[str, Any] | None:
    """按主键查 manifest：purge 完成后身份映射已删除，幂等重放只能按 id 定位。"""
    result = await session.execute(
        text(
            "SELECT * FROM account_deletion_manifest "
            "WHERE account_deletion_id = :account_deletion_id"
        ),
        {"account_deletion_id": account_deletion_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def update_manifest_status(
    session: AsyncSession,
    *,
    account_deletion_id: UUID,
    status: str,
    purge_completed_at: datetime | None = None,
    completion_proof_checksum: str | None = None,
) -> None:
    """原子推进 manifest 状态（§13.16）；行不存在时抛出以示异常。"""
    rowcount = await exec_rowcount(
        session,
        text(
            """
            UPDATE account_deletion_manifest
            SET status = :status,
                purge_completed_at = COALESCE(:purge_completed_at, purge_completed_at),
                completion_proof_checksum = COALESCE(:proof, completion_proof_checksum)
            WHERE account_deletion_id = :account_deletion_id
            """
        ),
        {
            "account_deletion_id": account_deletion_id,
            "status": status,
            "purge_completed_at": purge_completed_at,
            "proof": completion_proof_checksum,
        },
    )
    if rowcount != 1:
        raise LookupError(f"manifest 不存在: {account_deletion_id}")


async def list_manifests_for_replay(session: AsyncSession) -> list[dict[str, Any]]:
    """恢复重放候选：所有非 failed 的 manifest（§21.4：重新应用账号删除）。"""
    result = await session.execute(
        text(
            "SELECT * FROM account_deletion_manifest "
            "WHERE status IN ('requested', 'running', 'completed')"
        )
    )
    return [dict(row) for row in result.mappings().all()]


async def ensure_ops_schema(session: AsyncSession) -> None:
    """幂等创建 ops schema 与 ledger（恢复流程在 pg_restore 后调用）。"""
    for statement in OPS_ENSURE_DDL:
        await session.execute(text(statement))


async def upsert_ledger_entry(
    session: AsyncSession,
    *,
    account_deletion_id: UUID,
    user_hash: str,
    user_hash_key_version: str,
    status: str,
    requested_at: datetime,
    purge_completed_at: datetime | None = None,
    completion_proof_checksum: str | None = None,
) -> None:
    """写透 ledger：manifest 的每次状态变化同步到 ops schema（评审 P0-2）。"""
    await session.execute(
        text(
            """
            INSERT INTO ops.account_deletion_ledger (
                account_deletion_id, user_hash, user_hash_key_version,
                status, requested_at, purge_completed_at, completion_proof_checksum
            ) VALUES (
                :account_deletion_id, :user_hash, :user_hash_key_version,
                :status, :requested_at, :purge_completed_at, :proof
            )
            ON CONFLICT (account_deletion_id) DO UPDATE SET
                status = EXCLUDED.status,
                purge_completed_at = COALESCE(
                    EXCLUDED.purge_completed_at,
                    ops.account_deletion_ledger.purge_completed_at
                ),
                completion_proof_checksum = COALESCE(
                    EXCLUDED.completion_proof_checksum,
                    ops.account_deletion_ledger.completion_proof_checksum
                ),
                updated_at = now()
            """
        ),
        {
            "account_deletion_id": account_deletion_id,
            "user_hash": user_hash,
            "user_hash_key_version": user_hash_key_version,
            "status": status,
            "requested_at": requested_at,
            "purge_completed_at": purge_completed_at,
            "proof": completion_proof_checksum,
        },
    )


async def list_ledger_entries(session: AsyncSession) -> list[dict[str, Any]]:
    """读取 ledger 全部条目；ops schema 不存在（旧环境）时返回空列表。"""
    result = await session.execute(
        text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = 'ops' AND table_name = 'account_deletion_ledger'"
        )
    )
    if int(result.scalar_one()) == 0:
        return []
    rows = await session.execute(
        text("SELECT * FROM ops.account_deletion_ledger ORDER BY requested_at ASC")
    )
    return [dict(row) for row in rows.mappings().all()]


async def insert_privacy_audit(
    session: AsyncSession,
    *,
    privacy_audit_id: UUID,
    user_hash: str,
    user_hash_key_version: str,
    action: str,
    actor_hash: str | None,
    occurred_at: datetime,
    proof_checksum: str,
) -> None:
    """写入最小隐私审计记录（§13.16：长期保留，不含可还原用户正文）。"""
    await session.execute(
        text(
            """
            INSERT INTO memory_privacy_audit_records (
                privacy_audit_id, user_hash, user_hash_key_version,
                action, actor_hash, occurred_at, proof_checksum
            ) VALUES (
                :privacy_audit_id, :user_hash, :user_hash_key_version,
                :action, :actor_hash, :occurred_at, :proof_checksum
            )
            """
        ),
        {
            "privacy_audit_id": privacy_audit_id,
            "user_hash": user_hash,
            "user_hash_key_version": user_hash_key_version,
            "action": action,
            "actor_hash": actor_hash,
            "occurred_at": occurred_at,
            "proof_checksum": proof_checksum,
        },
    )
