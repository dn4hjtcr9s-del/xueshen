"""认证服务 HTTP 路由（方案 §4.1）：/api/v1/auth 下的注册与登录。

- register：users + identity_mapping_outbox 同事务落库（§3.2），201 + user。
- login：identifier 同时支持用户名或邮箱；成功签发 access token（RS256）并
  下发 refresh Cookie（family 独立，轮换/撤销逻辑见步骤 5）。
- refresh / logout / me 在会话管理步骤中挂载。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from backend.auth_service import ratelimit as rl
from backend.auth_service.database import (
    get_user_by_email,
    get_user_by_username,
    insert_outbox_event,
    insert_refresh_token,
    insert_user,
    refresh_expiry,
)
from backend.auth_service.errors import (
    auth_db_unavailable,
    invalid_credentials,
    rate_limited,
)
from backend.auth_service.runtime import AuthRuntime
from backend.auth_service.security import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    new_refresh_token,
    normalize_email,
    normalize_username,
    refresh_token_hash,
    validate_email,
    validate_password,
    validate_username,
    verify_password,
)

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


def _trace_id(request: Request) -> str:
    trace_id: str | None = getattr(request.state, "trace_id", None)
    return trace_id or ""


def _user_view(row: dict[str, Any]) -> dict[str, object]:
    """用户行 → 公开视图（方案 §4.1：user_id/username/email/status/created_at）。"""
    return {
        "user_id": str(row["user_id"]),
        "username": row["username"],
        "email": row["email"],
        "status": row["status"],
        "created_at": row["created_at"].isoformat() if row["created_at"] is not None else None,
    }


def _refresh_cookie(response: JSONResponse, token: str, *, secure: bool) -> None:
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


def _delete_refresh_cookie(response: JSONResponse, *, secure: bool) -> None:
    response.delete_cookie(
        key=REFRESH_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        path=REFRESH_COOKIE_PATH,
        secure=secure,
    )


def _cookie_secure(runtime: AuthRuntime) -> bool:
    return runtime.settings.app_env in ("staging", "production")


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
    password_hash = hash_password(body.password)

    user_id = uuid4()
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

    async with runtime.session_factory() as session:
        row = await get_user_by_username(session, account)
        if row is None:
            row = await get_user_by_email(session, normalize_email(identifier))

    stored_hash: str | None = row["password_hash"] if row is not None else None
    password_ok = verify_password(body.password, stored_hash or DUMMY_PASSWORD_HASH)
    status_ok = row is not None and row["status"] == "active"
    if not password_ok or not status_ok:
        if ip is not None:
            limiter.hit("auth_login_ip", ip, rl.LOGIN_FAIL_PER_IP)
        limiter.hit("auth_login_account", account, rl.LOGIN_FAIL_PER_ACCOUNT)
        raise invalid_credentials()

    assert row is not None
    limiter.clear("auth_login_account", account)

    family_id = uuid4()
    refresh_token = new_refresh_token()
    async with runtime.session_factory() as session:
        async with session.begin():
            await insert_refresh_token(
                session,
                token_hash=refresh_token_hash(refresh_token),
                user_id=row["user_id"],
                family_id=family_id,
                expires_at=refresh_expiry(datetime.now(UTC)),
            )

    access_token = runtime.issuer.issue(user_id=row["user_id"])
    response = JSONResponse(
        content={
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": 300,
            "user": _user_view(row),
        }
    )
    _refresh_cookie(response, refresh_token, secure=_cookie_secure(runtime))
    return response
