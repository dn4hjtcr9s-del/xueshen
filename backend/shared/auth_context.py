"""跨域共享：FastAPI 认证依赖（get_auth_context + require）（方案 §18.1–§18.3 / D29）。

从 backend/memory/api/dependencies.py 提取（D29 批准，超 D24 原范围）：
- 只读取 app.state.settings / app.state.runtime.session_factory 与 app.state
  注入的适配器（身份映射工厂、break-glass 存储/校验器），不 import 任何
  memory/auth 域实现模块（D29 分层：shared 只碰 app.state）；
- Memory 侧 backend/memory/api/dependencies.py 原位置 re-export；
- Community 只依赖本模块 + backend.auth.*，不反向依赖 backend.memory.api。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from collections.abc import Callable, Collection
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.context import (
    ALL_SCOPES,
    SCOPE_MEMORY_BREAK_GLASS,
    AuthContext,
)
from backend.auth.verifier import (
    AuthError,
    CompositeAuthVerifier,
    DevelopmentAuthAdapter,
    ProductionJwtAuthAdapter,
)
from backend.settings import Settings

#: Break-glass grant 请求头（§13.15）
BREAK_GLASS_HEADER = "x-break-glass-grant-id"

logger = logging.getLogger("shared.auth_context")


class IdentityMappingResolver(Protocol):
    """(issuer, external_subject) -> 内部 user_id；由 app 装配注入实现（D29）。"""

    async def resolve(self, *, issuer: str, external_subject: str) -> UUID | None: ...


class BreakGlassStore(Protocol):
    """break-glass 审计/授权存储；由 app 装配注入实现（D29）。"""

    async def get_grant(self, session: AsyncSession, grant_id: UUID) -> dict[str, Any] | None: ...

    async def insert_audit(
        self,
        session: AsyncSession,
        *,
        audit_id: UUID,
        grant_id: UUID,
        admin_user_id: UUID,
        target_user_id: UUID,
        action: str,
        resource_type: str,
        resource_id: UUID | None,
        trace_id: str,
    ) -> None: ...


class AuthRuntimeUnavailableError(RuntimeError):
    """API 运行时未初始化（路由只在 runtime 初始化后挂载）。

    与原 backend/memory/api/dependencies.py 的 DatabaseUnavailableError
    （503 DATABASE_UNAVAILABLE）语义等价；app.py 注册 handler 统一输出，
    Memory/Conversation/Community 各域共用同一认证入口。
    """


def _user_log_hash(key: str, user_id: str) -> str:
    """日志用哈希化用户标识（与 memory/contracts/common.py::user_log_hash 同构）。"""
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    message = f"user:v1:{user_id}".encode()
    return hmac.new(key_bytes, message, hashlib.sha256).hexdigest()


def _settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def _runtime_session_factory(request: Request) -> Any:
    """从 app.state.runtime 获取 session_factory（duck typing，不依赖具体类型）。

    runtime 缺失属于应用启动错误（路由只在 runtime 初始化后挂载），
    以 AuthRuntimeUnavailableError（503）表达，由 app.py handler 统一输出。
    """
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        raise AuthRuntimeUnavailableError("API 运行时尚未初始化")
    return runtime.session_factory


def _trace_id(request: Request) -> str:
    """读取 middleware 注入的 trace_id，缺失时生成 32 位十六进制（与 common.py 一致）。"""
    trace_id: str | None = getattr(request.state, "trace_id", None)
    return trace_id or secrets.token_hex(16)


async def get_auth_context(request: Request) -> AuthContext:
    """认证入口：development 优先 dev auth，其余走生产 JWT（§18.1）。

    携带 X-Break-Glass-Grant-Id 时走 §13.15 校验：限 admin、限 grant 属主、
    限时、限目标用户；校验通过则本次请求以目标用户身份执行并写使用审计。

    D29：身份映射与 break-glass 存储经 app.state 注入（identity_resolver_factory /
    break_glass_store / break_glass_validator），本模块不 import 实现。
    """
    settings = _settings(request)
    session_factory = _runtime_session_factory(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    # Dev Auth 来源限制（§18.1 / 评审 #9）：把客户端地址传给认证适配器
    client_host = request.client.host if request.client else None
    async with session_factory() as session:
        resolver_factory: Callable[[AsyncSession], IdentityMappingResolver] | None = getattr(
            request.app.state, "identity_resolver_factory", None
        )
        resolver = resolver_factory(session) if resolver_factory is not None else None
        verifier = CompositeAuthVerifier(
            settings=settings,
            dev_adapter=DevelopmentAuthAdapter(settings),
            prod_adapter=ProductionJwtAuthAdapter(
                settings=settings,
                identity_resolver=resolver,  # type: ignore[arg-type]  # D29：缺失时 dev 路径可用
            ),
        )
        auth = await verifier.authenticate(headers, client_host=client_host)
        raw_grant = headers.get(BREAK_GLASS_HEADER)
        if raw_grant is None:
            return auth
        return await _apply_break_glass(request, settings, session, auth, raw_grant)


async def get_optional_auth_context(request: Request) -> AuthContext | None:
    """可选认证依赖（D46）：无凭证 → 匿名 None；凭证无效/过期 → 401。

    空白 Authorization 头按 strip 后空串处理 → 匿名（写接口行为不变仍 401）。
    """
    settings = _settings(request)
    session_factory = _runtime_session_factory(request)
    headers = {k.lower(): v for k, v in request.headers.items()}
    client_host = request.client.host if request.client else None

    authz = headers.get("authorization")
    if authz is not None and authz.strip() == "":
        # 显式空白 Authorization：视为无凭证；是否还有 X-Dev-User-Id 由 verifier 决定
        headers.pop("authorization", None)
        authz = None

    # 无 Authorization 头：生产环境直接匿名；development 下无 X-Dev-User-Id 也匿名
    if authz is None:
        if not settings.is_development:
            return None
        if headers.get("x-dev-user-id") is None:
            return None

    async with session_factory() as session:
        resolver_factory: Callable[[AsyncSession], IdentityMappingResolver] | None = getattr(
            request.app.state, "identity_resolver_factory", None
        )
        resolver = resolver_factory(session) if resolver_factory is not None else None
        verifier = CompositeAuthVerifier(
            settings=settings,
            dev_adapter=DevelopmentAuthAdapter(settings),
            prod_adapter=ProductionJwtAuthAdapter(
                settings=settings,
                identity_resolver=resolver,  # type: ignore[arg-type]
            ),
        )
        try:
            auth = await verifier.authenticate(headers, client_host=client_host)
        except AuthError:
            raise
        raw_grant = headers.get(BREAK_GLASS_HEADER)
        if raw_grant is None:
            return auth
        return await _apply_break_glass(request, settings, session, auth, raw_grant)


async def _apply_break_glass(
    request: Request,
    settings: Settings,
    session: AsyncSession,
    auth: AuthContext,
    raw_grant: str,
) -> AuthContext:
    """校验 break-glass grant 并构造以目标用户身份执行的 AuthContext（§13.15）。"""
    trace_id = _trace_id(request)
    if auth.actor_type != "admin" or not auth.has_scope(SCOPE_MEMORY_BREAK_GLASS):
        raise AuthError(
            "AUTH_FORBIDDEN",
            "break-glass 仅限持有 memory:break_glass scope 的 admin 使用",
            forbidden=True,
        )
    try:
        grant_id = UUID(raw_grant)
    except ValueError as exc:
        raise AuthError("AUTH_FORBIDDEN", "grant_id 不是合法 UUID", forbidden=True) from exc

    store: BreakGlassStore | None = getattr(request.app.state, "break_glass_store", None)
    validator: Callable[..., str | None] | None = getattr(
        request.app.state, "break_glass_validator", None
    )
    if store is None or validator is None:
        raise AuthError("AUTH_FORBIDDEN", "break-glass 能力未装配", forbidden=True)

    grant = await store.get_grant(session, grant_id)
    now = datetime.now(UTC)
    reason = validator(settings=settings, grant=grant, admin_user_id=auth.user_id, now=now)
    if reason is not None:
        if reason == "expired" and grant is not None:
            await store.insert_audit(
                session,
                audit_id=uuid4(),
                grant_id=grant["grant_id"],
                admin_user_id=grant["admin_user_id"],
                target_user_id=grant["target_user_id"],
                action="expired_check",
                resource_type="auth_context",
                resource_id=None,
                trace_id=trace_id,
            )
            await session.commit()
        raise AuthError("AUTH_FORBIDDEN", f"break-glass grant 不可用: {reason}", forbidden=True)
    assert grant is not None  # reason is None 时 grant 必然存在

    await store.insert_audit(
        session,
        audit_id=uuid4(),
        grant_id=grant["grant_id"],
        admin_user_id=grant["admin_user_id"],
        target_user_id=grant["target_user_id"],
        action="use",
        resource_type="auth_context",
        resource_id=None,
        trace_id=trace_id,
    )
    await session.commit()
    request.state.break_glass = {
        "grant_id": grant["grant_id"],
        "admin_user_id": auth.user_id,
        "target_user_id": grant["target_user_id"],
    }
    logger.info(
        "break-glass 使用: admin=%s target=%s grant=%s",
        _user_log_hash(settings.log_hmac_key, str(auth.user_id)),
        _user_log_hash(settings.log_hmac_key, str(grant["target_user_id"])),
        grant["grant_id"],
    )
    scopes = frozenset(s for s in grant["scopes"] if s in ALL_SCOPES)
    return AuthContext(
        user_id=grant["target_user_id"],
        actor_type="admin",
        scopes=scopes,
        issuer=auth.issuer,
        external_subject=auth.external_subject,
        break_glass_grant_id=grant["grant_id"],
    )


def require(
    *,
    actors: Collection[str],
    scope: str | None = None,
    any_scopes: tuple[str, ...] = (),
) -> Any:
    """权限矩阵依赖工厂：actor_type 白名单 + scope 检查（§18.2/§18.3）。

    scope 为必须持有的单个 scope；any_scopes 非空时持有其一即可。
    """

    async def _dep(auth: AuthContext = Depends(get_auth_context)) -> AuthContext:
        if auth.actor_type not in actors:
            # §13.15：持有效 break-glass grant 的 admin 以目标用户身份放行
            if not (auth.actor_type == "admin" and auth.break_glass_grant_id is not None):
                raise AuthError(
                    "AUTH_FORBIDDEN",
                    f"actor_type={auth.actor_type} 无权访问该接口",
                    forbidden=True,
                )
        if scope is not None and not auth.has_scope(scope):
            raise AuthError("AUTH_FORBIDDEN", f"缺少 scope: {scope}", forbidden=True)
        if any_scopes and not any(auth.has_scope(s) for s in any_scopes):
            raise AuthError(
                "AUTH_FORBIDDEN", f"缺少 scope（任一即可）: {list(any_scopes)}", forbidden=True
            )
        return auth

    return _dep
