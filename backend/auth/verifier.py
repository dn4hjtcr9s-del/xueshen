"""认证适配器（规格 §18.1）。

- ProductionJwtAuthAdapter：校验网站统一认证签发的短时 JWT，并通过
  (issuer, sub) 查询 account_identity_mappings 解析内部 user_id。
- DevelopmentAuthAdapter：仅限 development 的测试身份，读取 X-Dev-User-Id。
"""

from __future__ import annotations

import asyncio
import ipaddress
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from uuid import UUID

import jwt

from backend.auth.context import (
    ACTOR_TYPES,
    AGENT_ACTOR_TYPES,
    AGENT_ALLOWED_SCOPES,
    ALL_SCOPES,
    DEFAULT_AUTH_ISSUER,
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
    async def authenticate(
        self, headers: dict[str, str], *, client_host: str | None = None
    ) -> AuthContext: ...


def _header(headers: dict[str, str], name: str) -> str | None:
    return headers.get(name.lower())


# ASGI 测试客户端（httpx ASGITransport）的默认 client host；进程内测试视为可信
_TESTCLIENT_HOST = "testclient"


def _is_dev_auth_source_allowed(client_host: str | None) -> bool:
    """Dev Auth 来源限制（§18.1 / 评审 #9）：仅 loopback 或 Compose/RFC1918 内网。

    拿不到客户端地址时 fail-closed 拒绝；ASGI 进程内测试客户端显式放行。
    """
    if client_host is None:
        return False
    if client_host == _TESTCLIENT_HOST:
        return True
    try:
        ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    # is_private 覆盖 RFC1918（含 Docker 网桥 172.16/12）与链路本地地址
    return ip.is_loopback or ip.is_private


@dataclass
class DevelopmentAuthAdapter:
    """本地开发测试身份。生产环境一律拒绝。"""

    settings: Settings

    async def authenticate(
        self, headers: dict[str, str], *, client_host: str | None = None
    ) -> AuthContext:
        if not (self.settings.is_development and self.settings.dev_auth_enabled):
            raise AuthError("AUTH_REQUIRED", "开发身份适配器未启用")
        if not _is_dev_auth_source_allowed(client_host):
            raise AuthError(
                "AUTH_FORBIDDEN",
                "Dev Auth 仅允许 loopback / 内网来源",
                forbidden=True,
            )
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
    - Agent actor（conversation/activity）必须带 delegated_sub（§18.4，评审 #15）：
      user_id 取委托用户，actor_principal 保留服务主体 sub，scope 不得越界。
    """

    settings: Settings
    identity_resolver: IdentityMappingResolver

    async def authenticate(
        self, headers: dict[str, str], *, client_host: str | None = None
    ) -> AuthContext:
        # client_host 仅用于 Dev Auth 来源限制，生产 JWT 不依赖来源地址
        authorization = _header(headers, "authorization")
        if not authorization or not authorization.startswith("Bearer "):
            raise AuthError("AUTH_REQUIRED", "缺少 Bearer token")
        token = authorization.removeprefix("Bearer ").strip()

        decode_key: object
        if self.settings.auth_public_key_file:
            # 文件优先（方案 §6.2）；读取失败直接抛 AuthError 而非静默回退
            try:
                decode_key = await asyncio.to_thread(
                    Path(self.settings.auth_public_key_file).read_text, encoding="utf-8"
                )
            except OSError as exc:
                raise AuthError("AUTH_REQUIRED", f"公钥文件读取失败: {type(exc).__name__}") from exc
        elif self.settings.auth_public_key:
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
                # 复审 P3：与签发端使用同一 fallback，issuer 缺省时不静默跳过 iss 校验
                issuer=self.settings.auth_issuer or DEFAULT_AUTH_ISSUER,
                # 评审二轮 #4：actor_type/scopes 同为必需 claims，缺失即拒
                options={
                    "require": ["iss", "aud", "sub", "iat", "exp", "jti", "actor_type", "scopes"]
                },
            )
        except jwt.PyJWTError as exc:
            raise AuthError("AUTH_REQUIRED", f"JWT 校验失败: {type(exc).__name__}") from exc

        iat = int(claims.get("iat", 0))
        exp = int(claims.get("exp", 0))
        if exp - iat > self.settings.auth_token_max_lifetime_seconds:
            raise AuthError("AUTH_REQUIRED", "token 有效期超过 5 分钟上限")
        if exp < time.time():
            raise AuthError("AUTH_REQUIRED", "token 已过期")

        # 严格 claims schema（评审二轮 #4）：类型错误或未知值整体拒绝 token，
        # 不静默修正、不静默过滤
        actor_raw = claims["actor_type"]
        if not isinstance(actor_raw, str) or actor_raw not in ACTOR_TYPES:
            raise AuthError("AUTH_REQUIRED", "actor_type claim 非法")
        actor = cast(ActorType, actor_raw)
        scopes_raw = claims["scopes"]
        if not isinstance(scopes_raw, list) or not all(isinstance(s, str) for s in scopes_raw):
            raise AuthError("AUTH_REQUIRED", "scopes claim 必须是字符串数组")
        unknown_scopes = set(scopes_raw) - ALL_SCOPES
        if unknown_scopes:
            raise AuthError("AUTH_REQUIRED", f"未知 scope: {sorted(unknown_scopes)}")
        scopes = frozenset(scopes_raw)

        issuer = str(claims["iss"])
        subject = str(claims["sub"])
        delegated_sub = claims.get("delegated_sub")
        if actor in AGENT_ACTOR_TYPES:
            # Agent 委托契约（§18.4，评审 #15）：必须带 delegated_sub 指向委托用户，
            # scope 不得超过 Agent 允许集；actor_principal 保留服务主体 sub。
            if not delegated_sub or not isinstance(delegated_sub, str):
                raise AuthError("AUTH_REQUIRED", "Agent token 缺少 delegated_sub claim")
            overreach = scopes - AGENT_ALLOWED_SCOPES
            if overreach:
                raise AuthError(
                    "AUTH_FORBIDDEN",
                    f"Agent scope 越界: {sorted(overreach)}",
                    forbidden=True,
                )
            user_id = await self.identity_resolver.resolve(
                issuer=issuer, external_subject=delegated_sub
            )
            if user_id is None:
                raise AuthError("AUTH_REQUIRED", "身份映射不存在")
            return AuthContext(
                user_id=user_id,
                actor_type=actor,
                scopes=scopes,
                issuer=issuer,
                external_subject=delegated_sub,
                actor_principal=subject,
            )
        if delegated_sub is not None:
            # 评审二轮 #4：非 Agent token 携带委托 claim 一律拒绝（防混淆代理）
            raise AuthError("AUTH_REQUIRED", "非 Agent token 不允许携带 delegated_sub")
        user_id = await self.identity_resolver.resolve(issuer=issuer, external_subject=subject)
        if user_id is None:
            raise AuthError("AUTH_REQUIRED", "身份映射不存在")
        return AuthContext(
            user_id=user_id,
            actor_type=actor,
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

    async def authenticate(
        self, headers: dict[str, str], *, client_host: str | None = None
    ) -> AuthContext:
        if self.settings.is_development and self.settings.dev_auth_enabled:
            if _header(headers, "authorization"):
                return await self.prod_adapter.authenticate(headers, client_host=client_host)
            return await self.dev_adapter.authenticate(headers, client_host=client_host)
        return await self.prod_adapter.authenticate(headers, client_host=client_host)
