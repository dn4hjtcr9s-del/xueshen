"""认证服务运行时依赖（方案 §2.3 / §6.3）：与 memory 运行时并列的独立装配。

auth 运行时持有最小权限 auth 库连接池与 access token 签发器；在
backend/app.py startup 阶段构建并挂到 app.state.auth_runtime。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.auth_service.database import AuthDatabase
from backend.auth_service.tokens import AccessTokenIssuer
from backend.settings import Settings


@dataclass
class AuthRuntime:
    """auth 库连接池 + token 签发器；与 memory 的 ApiRuntime 完全分离。"""

    settings: Settings
    database: AuthDatabase
    session_factory: async_sessionmaker[AsyncSession]
    issuer: AccessTokenIssuer


def build_auth_runtime(settings: Settings) -> AuthRuntime:
    database = AuthDatabase(settings)
    return AuthRuntime(
        settings=settings,
        database=database,
        session_factory=database.session_factory,
        issuer=AccessTokenIssuer(settings),
    )
