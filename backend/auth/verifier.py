"""认证适配器（规格 §18.1）。

- ProductionJwtAuthAdapter：校验网站统一认证签发的短时 JWT，并通过
  (issuer, sub) 查询 account_identity_mappings 解析内部 user_id。
- DevelopmentAuthAdapter：仅限 development 的测试身份，读取 X-Dev-User-Id。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

import jwt

from backend.auth.context import (
    ALL_SCOPES,
    DEV_USER_DEFAULT_SCOPES,
    ActorType,
    AuthContext,
)
from backend.settings import Settings


class AuthError(Exception):
    """认证/授权失败。code 为公开错误码。"""

    def __init__(self, code: str, message: str, *, forbidden: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.forbidden = forbidden


class IdentityMappingResolver(Protocol):
    """(issuer, external_subject) -> 内部 user_id。由持久层实现（步骤 4 接入）。"""

    async def resolve(self, *, issuer: str, external_subject: str) -> UUID | None: ...


class AuthVerifier(Protocol):
    async def authenticate(self, headers: dict[str, str]) -> AuthContext: ...


def _header(headers: dict[str, str], name: str) -> str | None:
    return headers.get(name.lower())


@dataclass
class DevelopmentAuthAdapter:
    """本地开发测试身份。生产环境一律拒绝。"""

    settings: Settings

    async def authenticate(self, headers: dict[str, str]) -> AuthContext:
        if not (self.settings.is_development and self.settings.dev_auth_enabled):
            raise AuthError("AUTH_REQUIRED", "开发身份适配器未启用")
        raw_user = _header(headers, "x-dev-user-id")
        if not raw_user:
            raise AuthError("AUTH_REQUIRED", "缺少 X-Dev-User-Id")
        try:
            user_id = UUID(raw_user)
        except ValueError as exc:
            raise AuthError("AUTH_REQUIRED", "X-Dev-User-Id 不是合法 UUID") from exc

        actor_type: ActorType = "user"
        scopes = DEV_USER_DEFAULT_SCOPES
        if self.settings.dev_auth_allow_scope_override:
            raw_actor = _header(headers, "x-dev-actor-type")
            if raw_actor in (
                "user",
                "conversation_agent",
                "activity_agent",
                "knowledge_graph_ui",
                "summary_projection",
                "system",
                "admin",
            ):
                actor_type = raw_actor  # type: ignore[assignment]
            raw_scopes = _header(headers, "x-dev-scopes")
            if raw_scopes:
                requested = frozenset(s for s in raw_scopes.split() if s)
                unknown = requested - ALL_SCOPES
                if unknown:
                    raise AuthError(
                        "AUTH_FORBIDDEN", f"未知 scope: {sorted(unknown)}", forbidden=True
                    )
                scopes = requested
        # 未开启 override 时，忽略 X-Dev-Actor-Type / X-Dev-Scopes，拒绝提权
        return AuthContext(user_id=user_id, actor_type=actor_type, scopes=scopes)


@dataclass
class ProductionJwtAuthAdapter:
    """网站统一认证 JWT 校验。

    - JWT 至少包含 iss/aud/sub/actor_type/scopes/iat/exp/jti。
    - token 最长有效期 5 分钟。
    - sub 通过身份映射解析为内部 UUID；映射不存在返回 401。
    """

    settings: Settings
    identity_resolver: IdentityMappingResolver

    async def authenticate(self, headers: dict[str, str]) -> AuthContext:
        authorization = _header(headers, "authorization")
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthError("AUTH_REQUIRED", "缺少 Bearer token")
        token = authorization.removeprefix("Bearer ").strip()

        decode_key: object
        if self.settings.auth_public_key:
            decode_key = self.settings.auth_public_key
        elif self.settings.auth_jwks_url:
            client = jwt.PyJWKClient(self.settings.auth_jwks_url)
            decode_key = client.get_signing_key_from_jwt(token).key
        else:
            raise AuthError("AUTH_REQUIRED", "生产认证参数未配置")

        try:
            claims = jwt.decode(
                token,
                decode_key,  # type: ignore[arg-type]
                algorithms=["RS256", "ES256"],
                audience=self.settings.auth_audience,
                issuer=self.settings.auth_issuer,
                options={"require": ["iss", "aud", "sub", "iat", "exp", "jti"]},
            )
        except jwt.PyJWTError as exc:
            raise AuthError("AUTH_REQUIRED", f"JWT 校验失败: {type(exc).__name__}") from exc

        iat = int(claims.get("iat", 0))
        exp = int(claims.get("exp", 0))
        if exp - iat > self.settings.auth_token_max_lifetime_seconds:
            raise AuthError("AUTH_REQUIRED", "token 有效期超过 5 分钟上限")
        if exp < time.time():
            raise AuthError("AUTH_REQUIRED", "token 已过期")

        actor_raw = claims.get("actor_type", "user")
        if actor_raw not in (
            "user",
            "conversation_agent",
            "activity_agent",
            "knowledge_graph_ui",
            "summary_projection",
            "system",
            "admin",
        ):
            raise AuthError("AUTH_FORBIDDEN", "非法 actor_type", forbidden=True)
        scopes_raw = claims.get("scopes", [])
        scopes = frozenset(s for s in scopes_raw if s in ALL_SCOPES)

        issuer = str(claims["iss"])
        subject = str(claims["sub"])
        user_id = await self.identity_resolver.resolve(issuer=issuer, external_subject=subject)
        if user_id is None:
            raise AuthError("AUTH_REQUIRED", "身份映射不存在")
        return AuthContext(
            user_id=user_id,
            actor_type=actor_raw,
            scopes=scopes,
            issuer=issuer,
            external_subject=subject,
        )


@dataclass
class CompositeAuthVerifier:
    """按环境选择认证适配器：development 优先 Dev Auth，其余使用生产 JWT。"""

    settings: Settings
    dev_adapter: DevelopmentAuthAdapter
    prod_adapter: ProductionJwtAuthAdapter

    async def authenticate(self, headers: dict[str, str]) -> AuthContext:
        if self.settings.is_development and self.settings.dev_auth_enabled:
            if _header(headers, "authorization"):
                return await self.prod_adapter.authenticate(headers)
            return await self.dev_adapter.authenticate(headers)
        return await self.prod_adapter.authenticate(headers)
