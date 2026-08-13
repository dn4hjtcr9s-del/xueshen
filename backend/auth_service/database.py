"""认证服务数据库访问（方案 §2.1 / §5.1）：独立 auth 库引擎、会话工厂与仓储。

auth 库使用最小权限账号（auth），与 memory 库引擎完全独立。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.auth_service.errors import email_taken, username_taken
from backend.settings import Settings

#: refresh token 有效期（方案 §4.4）
REFRESH_TOKEN_TTL_DAYS = 30


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.auth_database_url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        connect_args={
            "options": (
                f"-c statement_timeout={settings.database_statement_timeout_ms} "
                f"-c lock_timeout={settings.database_lock_timeout_ms}"
            ),
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


class AuthDatabase:
    """auth 库访问入口：引擎、会话工厂与可用性探测。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.engine = create_engine(settings)
        self.session_factory = create_session_factory(self.engine)

    async def ping(self) -> bool:
        try:
            async with self.session_factory() as session:
                await session.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self.engine.dispose()


def _user_row_to_view(row: Any) -> dict[str, Any]:
    return {
        "user_id": str(row.user_id),
        "username": str(row.username),
        "email": str(row.email) if row.email is not None else None,
        "status": str(row.status),
        "created_at": row.created_at,
    }


async def insert_user(
    session: AsyncSession,
    *,
    user_id: UUID,
    username: str,
    email: str | None,
    password_hash: str,
) -> dict[str, Any]:
    """插入用户行；唯一冲突映射为 AUTH_USERNAME_TAKEN / AUTH_EMAIL_TAKEN。"""
    try:
        result = await session.execute(
            text(
                """
                INSERT INTO users (user_id, username, email, password_hash)
                VALUES (:user_id, :username, :email, :password_hash)
                RETURNING user_id, username, email, status, created_at
                """
            ),
            {
                "user_id": user_id,
                "username": username,
                "email": email,
                "password_hash": password_hash,
            },
        )
        row = result.one()
        return _user_row_to_view(row)
    except IntegrityError as exc:
        constraint = getattr(getattr(exc, "orig", None), "diag", None)
        constraint_name = getattr(constraint, "constraint_name", None)
        if constraint_name == "uq_users_username":
            raise username_taken() from exc
        if constraint_name == "uq_users_email_nonnull":
            raise email_taken() from exc
        raise


async def get_user_by_username(session: AsyncSession, username: str) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            """
            SELECT user_id, username, email, password_hash, status, created_at
            FROM users WHERE username = :username
            """
        ),
        {"username": username},
    )
    row = result.first()
    return dict(row._mapping) if row is not None else None


async def get_user_by_email(session: AsyncSession, email: str) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            """
            SELECT user_id, username, email, password_hash, status, created_at
            FROM users WHERE email = :email
            """
        ),
        {"email": email},
    )
    row = result.first()
    return dict(row._mapping) if row is not None else None


async def get_user_by_id(session: AsyncSession, user_id: UUID) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            """
            SELECT user_id, username, email, password_hash, status, created_at
            FROM users WHERE user_id = :user_id
            """
        ),
        {"user_id": user_id},
    )
    row = result.first()
    return dict(row._mapping) if row is not None else None


async def insert_outbox_event(
    session: AsyncSession,
    *,
    event_id: UUID,
    user_id: UUID,
    issuer: str,
    external_subject: str,
) -> None:
    """补偿事件与 users 同事务落库（方案 §3.2）。"""
    await session.execute(
        text(
            """
            INSERT INTO identity_mapping_outbox (
                event_id, user_id, issuer, external_subject
            ) VALUES (:event_id, :user_id, :issuer, :external_subject)
            """
        ),
        {
            "event_id": event_id,
            "user_id": user_id,
            "issuer": issuer,
            "external_subject": external_subject,
        },
    )


def refresh_expiry(now: datetime) -> datetime:
    """refresh token 过期时间：签发时刻 + 30 天（方案 §4.4）。"""
    return (now if now.tzinfo else now.replace(tzinfo=UTC)) + timedelta(days=REFRESH_TOKEN_TTL_DAYS)
