"""认证服务 HTTP 路由（方案 §4.1 / §4.4）：/api/v1/auth 下完整会话管理。

- register：users + identity_mapping_outbox 同事务落库（§3.2），201 + user。
- login：identifier 支持用户名或邮箱；成功签发 access token（RS256）并下发
  refresh Cookie；映射缺失时即时补建，补建失败返回 AUTH_MAPPING_PENDING（A.3 #10）。
- refresh：轮换 refresh token；已作废 token 复用 = 重放 → 撤销整族（§4.4）。
- logout：幂等 204，撤销当前 family。
- me：仅认 Bearer token，不查身份映射（注册后、补偿完成前也可用）。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import jwt
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from backend.auth_service import ratelimit as rl
from backend.auth_service.database import (
    get_user_by_email,
    get_user_by_id,
    get_user_by_username,
    insert_outbox_event,
    insert_user,
    refresh_expiry,
)
from backend.auth_service.errors import (
    auth_db_unavailable,
    invalid_credentials,
    mapping_pending,
    rate_limited,
    session_invalid,
)
from backend.auth_service.runtime import AuthRuntime
from backend.auth_service.security import (
    DUMMY_PASSWORD_HASH,
    hash_password_async,
    new_refresh_token,
    normalize_email,
    normalize_username,
    refresh_token_hash,
    validate_email,
    validate_password,
    validate_username,
    verify_password_async,
)
from backend.auth_service.session import (
    get_token_row_for_update,
    insert_refresh_token,
    revoke_all_families,
    revoke_family,
    rotate,
)

logger = logging.getLogger("memory.api")

router = APIRouter(tags=["auth"])

#: refresh Cookie（方案 §7 精确属性）
REFRESH_COOKIE_NAME = "gewu_refresh_token"
REFRESH_COOKIE_PATH = "/api/v1/auth"
REFRESH_COOKIE_MAX_AGE = 30 * 24 * 3600  # 30 天


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    email: str | None = None
    password: str


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str
    password: str


def get_auth_runtime(request: Request) -> AuthRuntime:
    runtime: AuthRuntime | None = getattr(request.app.state, "auth_runtime", None)
    if runtime is None:
        raise auth_db_unavailable()
    return runtime


def _user_view(row: dict[str, Any]) -> dict[str, object]:
    """用户行 → 公开视图（方案 §4.1：user_id/username/email/status/created_at）。"""
    return {
        "user_id": str(row["user_id"]),
        "username": row["username"],
        "email": row["email"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] is not None else None,
    }


def _token_response(runtime: AuthRuntime, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "access_token": runtime.issuer.issue(user_id=row["user_id"]),
        "token_type": "bearer",
        "expires_in": 300,
        "user": _user_view(row),
    }


def _refresh_cookie(response: Response, token: str, *, secure: bool) -> None:
    """设置 refresh Cookie；删除时使用完全相同的 Path（方案 §7）。"""
    response.set_cookie(
        key=REFRESH_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
        max_age=REFRESH_COOKIE_MAX_AGE,
        secure=secure,
    )


def _delete_refresh_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
        secure=secure,
    )


def _cookie_secure(runtime: AuthRuntime) -> bool:
    return runtime.settings.app_env in ("staging", "production")


def _family_log_ref(family_id: Any) -> str:
    """安全日志用 family 引用：哈希截断，不落明文（§4.4 重放日志）。"""
    return hashlib.sha256(str(family_id).encode("ascii")).hexdigest()[:16]


async def _ensure_identity_mapping(runtime: AuthRuntime, user_id: Any, issuer: str) -> None:
    """登录兜底（方案 §3.2 / 附录 A.3 #10）：映射缺失时即时补建，失败 503。

    评审 P1-6：补建后必须核对归属——(issuer, external_subject) 已指向其他
    内部用户时拒绝登录（fail-closed），不静默放行。
    """
    memory_factory = runtime.memory_session_factory
    if memory_factory is None:
        return
    try:
        async with memory_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        """
                        INSERT INTO account_identity_mappings (
                            internal_user_id, issuer, external_subject
                        ) VALUES (:user_id, :issuer, :sub)
                        ON CONFLICT (issuer, external_subject) DO NOTHING
                        """
                    ),
                    {"user_id": user_id, "issuer": issuer, "sub": str(user_id)},
                )
                result = await session.execute(
                    text(
                        "SELECT internal_user_id FROM account_identity_mappings "
                        "WHERE issuer = :issuer AND external_subject = :sub"
                    ),
                    {"issuer": issuer, "sub": str(user_id)},
                )
                existing = result.scalar_one_or_none()
    except Exception as exc:
        logger.error("登录时身份映射补建失败 user=%s: %s", user_id, type(exc).__name__)
        raise mapping_pending() from exc
    if existing is None or str(existing) != str(user_id):
        logger.error(
            "登录拒绝：身份映射 (issuer=%s, sub=%s) 指向其他内部用户 %s",
            issuer,
            user_id,
            existing,
        )
        raise mapping_pending()


@router.post("/register", status_code=201)
async def register(request: Request, body: RegisterRequest) -> JSONResponse:
    """注册新账号：同事务写 users + 补偿事件，映射异步建立（方案 §3.2）。"""
    runtime = get_auth_runtime(request)
    settings = runtime.settings

    ip = rl.client_ip(request, settings.auth_trusted_proxy_cidrs)
    limiter = request.app.state.rate_limiter
    if ip is not None and not limiter.hit("auth_register_ip", ip, rl.REGISTER_PER_IP):
        raise rate_limited(rl.retry_after_seconds())

    username = validate_username(body.username)
    email = validate_email(body.email) if body.email else None
    validate_password(body.password)
    password_hash = await hash_password_async(body.password)

    user_id = uuid4()
    try:
        async with runtime.session_factory() as session:
            async with session.begin():
                user = await insert_user(
                    session,
                    user_id=user_id,
                    username=username,
                    email=email,
                    password_hash=password_hash,
                )
                await insert_outbox_event(
                    session,
                    event_id=uuid4(),
                    user_id=user_id,
                    issuer=runtime.issuer.issuer,
                    external_subject=str(user_id),
                )
    except SQLAlchemyError as exc:
        raise auth_db_unavailable() from exc
    return JSONResponse(status_code=201, content={"user": _user_view(user)})


@router.post("/login")
async def login(request: Request, body: LoginRequest) -> JSONResponse:
    """登录：用户名/邮箱 + 密码；成功签发 access token 并下发 refresh Cookie。

    失败计数限流（方案 §10.1）：每 IP 10 次/分钟、每规范化账号 5 次/分钟；
    成功登录清除账号桶。disabled 账号与凭据错误统一返回 401，不泄露账号状态。
    """
    runtime = get_auth_runtime(request)
    settings = runtime.settings
    ip = rl.client_ip(request, settings.auth_trusted_proxy_cidrs)
    limiter = request.app.state.rate_limiter

    identifier = body.identifier.strip().lower()
    account = normalize_username(identifier)

    if ip is not None and limiter.is_limited("auth_login_ip", ip, rl.LOGIN_FAIL_PER_IP):
        raise rate_limited(rl.retry_after_seconds())
    if limiter.is_limited("auth_login_account", account, rl.LOGIN_FAIL_PER_ACCOUNT):
        raise rate_limited(rl.retry_after_seconds())

    try:
        async with runtime.session_factory() as session:
            row = await get_user_by_username(session, account)
            if row is None:
                row = await get_user_by_email(session, normalize_email(identifier))
    except SQLAlchemyError as exc:
        raise auth_db_unavailable() from exc

    stored_hash: str | None = row["password_hash"] if row is not None else None
    password_ok = await verify_password_async(body.password, stored_hash or DUMMY_PASSWORD_HASH)
    status_ok = row is not None and row["status"] == "active"
    if not password_ok or not status_ok:
        if ip is not None:
            limiter.hit("auth_login_ip", ip, rl.LOGIN_FAIL_PER_IP)
        limiter.hit("auth_login_account", account, rl.LOGIN_FAIL_PER_ACCOUNT)
        raise invalid_credentials()

    assert row is not None
    limiter.clear("auth_login_account", account)
    await _ensure_identity_mapping(runtime, row["user_id"], runtime.issuer.issuer)

    family_id = uuid4()
    refresh_token = new_refresh_token()
    try:
        async with runtime.session_factory() as session:
            async with session.begin():
                await insert_refresh_token(
                    session,
                    token_hash=refresh_token_hash(refresh_token),
                    user_id=row["user_id"],
                    family_id=family_id,
                    expires_at=refresh_expiry(datetime.now(UTC)),
                )
    except SQLAlchemyError as exc:
        raise auth_db_unavailable() from exc

    response = JSONResponse(content=_token_response(runtime, row))
    _refresh_cookie(response, refresh_token, secure=_cookie_secure(runtime))
    return response


@router.post("/refresh")
async def refresh(request: Request) -> JSONResponse:
    """轮换 refresh token（方案 §4.4）。

    - 缺失/无效/过期/已撤销/重放统一返回 401 AUTH_SESSION_INVALID；
    - 重放（已作废 token 复用）撤销整族，细节只写安全日志；
    - 用户 disabled 时撤销其全部 family。
    """
    runtime = get_auth_runtime(request)
    settings = runtime.settings
    ip = rl.client_ip(request, settings.auth_trusted_proxy_cidrs)
    limiter = request.app.state.rate_limiter
    if ip is not None and not limiter.hit("auth_refresh_ip", ip, rl.REFRESH_PER_IP):
        raise rate_limited(rl.retry_after_seconds())

    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw:
        raise session_invalid()

    now = datetime.now(UTC)
    new_token = new_refresh_token()
    decision: dict[str, Any] = {}
    try:
        async with runtime.session_factory() as session:
            async with session.begin():
                row = await get_token_row_for_update(session, refresh_token_hash(raw))
                if row is None:
                    decision["missing"] = True
                elif row["revoked_at"] is not None:
                    # 重放迹象：撤销整族，不设宽限期（方案 §4.4）。
                    # 撤销写与读取同事务提交，之后才抛业务错误（避免回滚副作用）。
                    await revoke_family(session, row["family_id"])
                    decision["replay"] = row["family_id"]
                elif row["expires_at"] is not None and row["expires_at"] < now:
                    decision["expired"] = True
                else:
                    user_row = await get_user_by_id(session, row["user_id"])
                    if user_row is None:
                        decision["missing"] = True
                    elif user_row["status"] == "disabled":
                        # 禁用账号：撤销其全部 family（方案 §4.4）
                        await revoke_all_families(session, row["user_id"])
                        decision["disabled"] = True
                    else:
                        await rotate(
                            session,
                            old_hash=refresh_token_hash(raw),
                            new_hash=refresh_token_hash(new_token),
                            user_id=row["user_id"],
                            family_id=row["family_id"],
                            now=now,
                        )
                        decision["user"] = user_row
    except SQLAlchemyError as exc:
        raise auth_db_unavailable() from exc

    if "replay" in decision:
        logger.warning("refresh 重放检测：撤销整族 family=%s", _family_log_ref(decision["replay"]))
    if "user" not in decision:
        raise session_invalid()

    user = decision["user"]
    response = JSONResponse(content=_token_response(runtime, user))
    _refresh_cookie(response, new_token, secure=_cookie_secure(runtime))
    return response


@router.post("/logout", status_code=204)
async def logout(request: Request) -> Response:
    """退出登录：撤销当前 family，删除 Cookie；幂等（方案 §4.1 / §4.4）。"""
    runtime = get_auth_runtime(request)
    raw = request.cookies.get(REFRESH_COOKIE_NAME)
    response = Response(status_code=204)
    _delete_refresh_cookie(response, secure=_cookie_secure(runtime))
    if raw:
        try:
            async with runtime.session_factory() as session:
                async with session.begin():
                    row = await get_token_row_for_update(session, refresh_token_hash(raw))
                    if row is not None and row["revoked_at"] is None:
                        await revoke_family(session, row["family_id"])
        except SQLAlchemyError as exc:
            raise auth_db_unavailable() from exc
    return response


@router.get("/me")
async def me(request: Request) -> JSONResponse:
    """当前用户信息（方案 §4.1）：仅认 Bearer token，不查身份映射。"""
    runtime = get_auth_runtime(request)
    authorization = request.headers.get("authorization")
    if not authorization or not authorization.startswith("Bearer "):
        raise session_invalid()
    token = authorization.removeprefix("Bearer ").strip()
    try:
        user_id = await runtime.verifier.verify_sub(token)
    except jwt.PyJWTError as exc:
        raise session_invalid() from exc

    try:
        async with runtime.session_factory() as session:
            row = await get_user_by_id(session, user_id)
    except SQLAlchemyError as exc:
        raise auth_db_unavailable() from exc
    if row is None:
        raise session_invalid()
    return JSONResponse(content=_user_view(row))
