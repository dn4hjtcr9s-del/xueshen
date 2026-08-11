"""身份映射仓储（规格 §13.1）：(issuer, external_subject) -> 内部 UUID。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class IdentityMappingRepository:
    """实现 auth.verifier.IdentityMappingResolver 协议。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(self, *, issuer: str, external_subject: str) -> UUID | None:
        result = await self._session.execute(
            text(
                "SELECT internal_user_id FROM account_identity_mappings "
                "WHERE issuer = :issuer AND external_subject = :external_subject"
            ),
            {"issuer": issuer, "external_subject": external_subject},
        )
        row = result.first()
        return UUID(str(row[0])) if row else None

    async def create(
        self,
        *,
        internal_user_id: UUID,
        issuer: str,
        external_subject: str,
        replace_existing: bool = False,
    ) -> dict[str, Any]:
        """受控维护 CLI 使用；默认冲突即拒绝（§13.1）。"""
        if replace_existing:
            await self._session.execute(
                text(
                    "DELETE FROM account_identity_mappings "
                    "WHERE issuer = :issuer AND external_subject = :external_subject"
                ),
                {"issuer": issuer, "external_subject": external_subject},
            )
        await self._session.execute(
            text(
                """
                INSERT INTO account_identity_mappings (
                    internal_user_id, issuer, external_subject
                ) VALUES (:internal_user_id, :issuer, :external_subject)
                """
            ),
            {
                "internal_user_id": internal_user_id,
                "issuer": issuer,
                "external_subject": external_subject,
            },
        )
        return {
            "internal_user_id": str(internal_user_id),
            "issuer": issuer,
            "external_subject": external_subject,
        }
