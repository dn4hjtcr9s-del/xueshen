"""公开用户资料读取（方案 §9.2 / D9）。

同进程 AuthRuntime 最小只读端口：内部使用 auth 库 session_factory 与
get_user_by_id()，但只投影 user_id/username/status（禁止 email/password
hash/refresh token 等敏感字段外流）。Community Repository 不接触 Auth
session factory（§9.2：adapter 边界隔离）。
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.auth_service.database import get_user_by_id
from backend.community.contracts.errors import CommunityNotFoundError


@dataclass(frozen=True)
class PublicUserProfile:
    """投影后的最小公开资料（§9.2：不含任何敏感字段）。"""

    user_id: UUID
    username: str
    status: str


class PublicUserProfileReader:
    """Auth 用户资料只读 adapter（§9.2 / D9）。"""

    def __init__(self, auth_session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._auth_session_factory = auth_session_factory

    async def get_active_profile(self, user_id: UUID) -> PublicUserProfile:
        """读取用户资料；用户不存在或非 active 视为创建前校验失败。

        创建帖子/回复前必须要求 status=active（§9.2）；profile 暂时不可用时
        创建请求失败并可安全重试，不把 UUID 当昵称。
        """
        async with self._auth_session_factory() as session:
            row = await get_user_by_id(session, user_id)
        if row is None:
            raise CommunityNotFoundError("用户不存在或无权访问")
        status = str(row.get("status", ""))
        if status != "active":
            raise CommunityNotFoundError("用户不存在或无权访问")
        return PublicUserProfile(
            user_id=user_id,
            username=str(row["username"]),
            status=status,
        )
