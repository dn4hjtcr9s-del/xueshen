"""认证服务运行时依赖（方案 §2.3 / §6.3）：与 memory 运行时并列的独立装配。

auth 运行时持有最小权限 auth 库连接池与 access token 签发器；在
backend/app.py startup 阶段构建并挂到 app.state.auth_runtime。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.auth_service.database import AuthDatabase
from backend.auth_service.tokens import AccessTokenIssuer, AccessTokenVerifier
from backend.settings import Settings


@dataclass
class AuthRuntime:
    """auth 库连接池 + token 签发/验签；与 memory 的 ApiRuntime 完全分离。

    memory_session_factory 供登录时的身份映射兜底补建（方案 §3.2）与补偿消费
    任务使用；未装配时（如纯 auth 测试）跳过映射相关逻辑。
    """

    settings: Settings
    database: AuthDatabase
    session_factory: async_sessionmaker[AsyncSession]
    issuer: AccessTokenIssuer
    verifier: AccessTokenVerifier
    memory_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_auth_runtime(
    settings: Settings,
    *,
    memory_session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> AuthRuntime:
    database = AuthDatabase(settings)
    return AuthRuntime(
        settings=settings,
        database=database,
        session_factory=database.session_factory,
        issuer=AccessTokenIssuer(settings),
        verifier=AccessTokenVerifier(settings),
        memory_session_factory=memory_session_factory,
    )
