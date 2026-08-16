"""Study 内部账号清理 API（D19/§12.8/§18.10）。

POST /api/v1/internal/study-accounts/purge：
- 仅允许 actor_type=system 且持 study:account_purge scope 的独立
  principal 调用（D19：system principal；fail-closed）；
- account_deletion_id 是幂等锚点：同一删除 ID 重放返回原结果，
  不创建第二次清理（ledger 主键天然去重）；
- 覆盖 Study 全部数据（§18.9 清单）+ 模型响应缓存，保留删除账本
  （study_account_purge_ledger）证明清理完成。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.context import SCOPE_STUDY_ACCOUNT_PURGE, AuthContext
from backend.shared.auth_context import require
from backend.study.api.dependencies import get_study_runtime
from backend.study.contracts.api import AccountPurgeRequest
from backend.study.persistence import repositories as repo

router = APIRouter(prefix="/api/v1/internal", tags=["internal-study"])

_SERVICE_ONLY = frozenset({"system"})


async def _upsert_ledger(
    session: AsyncSession,
    *,
    account_deletion_id: UUID,
    user_id: UUID,
    status: str,
    error_message: str | None = None,
    completed_at: datetime | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO study_account_purge_ledger (account_deletion_id, user_id, status,
                completed_at, error_message)
            VALUES (:did, :user_id, :status, :completed_at, :error_message)
            ON CONFLICT (account_deletion_id) DO UPDATE
            SET status = EXCLUDED.status,
                completed_at = COALESCE(EXCLUDED.completed_at,
                    study_account_purge_ledger.completed_at),
                error_message = EXCLUDED.error_message
            """
        ),
        {
            "did": account_deletion_id,
            "user_id": user_id,
            "status": status,
            "completed_at": completed_at,
            "error_message": error_message,
        },
    )


async def _ledger_row(session: AsyncSession, *, account_deletion_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text("SELECT * FROM study_account_purge_ledger WHERE account_deletion_id = :did"),
        {"did": account_deletion_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


@router.post("/study-accounts/purge")
async def purge_account(
    request: Request,
    payload: AccountPurgeRequest,
    auth: Annotated[
        AuthContext, Depends(require(actors=_SERVICE_ONLY, scope=SCOPE_STUDY_ACCOUNT_PURGE))
    ],
) -> JSONResponse:
    """按 account_deletion_id 幂等清理用户全部 Study 数据（D19/§12.8）。"""
    runtime = get_study_runtime(request)
    now = datetime.now(UTC)
    async with runtime.database.session_factory() as session:
        # 幂等锚：已完成/进行中的同 ID 直接返回既有状态（检查与执行同一事务）
        async with session.begin():
            existing = await _ledger_row(session, account_deletion_id=payload.account_deletion_id)
            if existing is not None and existing["status"] in ("succeeded", "running"):
                return JSONResponse(
                    status_code=200,
                    content={
                        "account_deletion_id": str(payload.account_deletion_id),
                        "status": str(existing["status"]),
                        "completed_at": (
                            existing["completed_at"].isoformat()
                            if existing["completed_at"] is not None
                            else None
                        ),
                    },
                )
            await _upsert_ledger(
                session,
                account_deletion_id=payload.account_deletion_id,
                user_id=payload.user_id,
                status="running",
            )
            await repo.purge_user_data(session, user_id=payload.user_id)
            await _upsert_ledger(
                session,
                account_deletion_id=payload.account_deletion_id,
                user_id=payload.user_id,
                status="succeeded",
                completed_at=now,
            )
        return JSONResponse(
            status_code=200,
            content={
                "account_deletion_id": str(payload.account_deletion_id),
                "status": "succeeded",
                "completed_at": now.isoformat(),
            },
        )
