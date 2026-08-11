"""backup_runs 仓储（规格 §13.14）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.persistence.database import exec_rowcount


async def insert_run(
    session: AsyncSession,
    *,
    batch_id: UUID,
    backup_root: str,
    postgres_artifact: str,
    markdown_artifact: str,
    manifest_artifact: str,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO backup_runs (
                batch_id, status, backup_root, postgres_artifact,
                markdown_artifact, manifest_artifact
            ) VALUES (
                :batch_id, 'running', :backup_root, :postgres_artifact,
                :markdown_artifact, :manifest_artifact
            )
            """
        ),
        {
            "batch_id": batch_id,
            "backup_root": backup_root,
            "postgres_artifact": postgres_artifact,
            "markdown_artifact": markdown_artifact,
            "manifest_artifact": manifest_artifact,
        },
    )


async def mark_succeeded(
    session: AsyncSession,
    *,
    batch_id: UUID,
    postgres_checksum: str,
    markdown_checksum: str,
    manifest_checksum: str,
    completed_at: datetime,
) -> None:
    await session.execute(
        text(
            """
            UPDATE backup_runs SET
                status = 'succeeded',
                postgres_checksum = :postgres_checksum,
                markdown_checksum = :markdown_checksum,
                manifest_checksum = :manifest_checksum,
                completed_at = :completed_at
            WHERE batch_id = :batch_id
            """
        ),
        {
            "batch_id": batch_id,
            "postgres_checksum": postgres_checksum,
            "markdown_checksum": markdown_checksum,
            "manifest_checksum": manifest_checksum,
            "completed_at": completed_at,
        },
    )


async def mark_failed(
    session: AsyncSession, *, batch_id: UUID, error_summary: str, completed_at: datetime
) -> None:
    await session.execute(
        text(
            """
            UPDATE backup_runs SET
                status = 'failed', error_summary = :error_summary,
                completed_at = :completed_at
            WHERE batch_id = :batch_id
            """
        ),
        {
            "batch_id": batch_id,
            "error_summary": error_summary[:1000],
            "completed_at": completed_at,
        },
    )


async def get_run(session: AsyncSession, batch_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM backup_runs WHERE batch_id = :batch_id"),
        {"batch_id": batch_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def mark_restore_verified(
    session: AsyncSession,
    *,
    batch_id: UUID,
    status: str,
    error: str | None,
    verified_at: datetime,
) -> bool:
    rowcount = await exec_rowcount(
        session,
        text(
            """
            UPDATE backup_runs SET
                restore_verification_status = :status,
                restore_verified_at = :verified_at,
                restore_verification_error = :error
            WHERE batch_id = :batch_id
            """
        ),
        {
            "batch_id": batch_id,
            "status": status,
            "error": error[:1000] if error else None,
            "verified_at": verified_at,
        },
    )
    return rowcount == 1
