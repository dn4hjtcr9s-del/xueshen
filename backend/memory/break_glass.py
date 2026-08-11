"""Break-glass 校验逻辑（规格 §13.15）。

grant 必须绑定唯一目标用户、必填 reason 和 scopes，最长有效期
`settings.break_glass_max_minutes` 分钟（默认 60），生产环境申请者与批准者
必须不同。校验失败原因以字符串返回，由调用方映射为 403 并写审计。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from backend.auth.context import ALL_SCOPES
from backend.settings import Settings

MAX_REASON_LENGTH = 500


def validate_grant_creation(
    *,
    settings: Settings,
    reason: str,
    scopes: list[str],
    expires_at: datetime,
    now: datetime,
    admin_user_id: UUID,
    approved_by: UUID | None,
) -> None:
    """创建 grant 前的校验；不合法抛 ValueError（CLI 映射为退出码 2）。"""
    if not settings.break_glass_enabled:
        raise ValueError("break-glass 已禁用（BREAK_GLASS_ENABLED=false）")
    if not reason.strip():
        raise ValueError("reason 必填")
    if len(reason) > MAX_REASON_LENGTH:
        raise ValueError(f"reason 超长（>{MAX_REASON_LENGTH} 字符）")
    if not scopes:
        raise ValueError("scopes 必填且非空")
    unknown = sorted(set(scopes) - ALL_SCOPES)
    if unknown:
        raise ValueError(f"未知 scope: {unknown}")
    if expires_at <= now:
        raise ValueError("expires_at 必须晚于当前时间")
    if expires_at - now > timedelta(minutes=settings.break_glass_max_minutes):
        raise ValueError(f"有效期超过上限 {settings.break_glass_max_minutes} 分钟（§13.15）")
    if settings.app_env == "production":
        if approved_by is None:
            raise ValueError("生产环境必须指定批准者 approved_by")
        if approved_by == admin_user_id:
            raise ValueError("生产环境申请者与批准者必须不同（§13.15）")


def validate_grant_for_use(
    *,
    settings: Settings,
    grant: dict[str, Any] | None,
    admin_user_id: UUID,
    now: datetime,
) -> str | None:
    """使用 grant 前的校验。返回 None 表示可用，否则返回拒绝原因。

    原因取值：disabled / not_found / wrong_admin / revoked / expired。
    "expired" 单独标识，调用方需写 expired_check 审计（§13.15）。
    """
    if not settings.break_glass_enabled:
        return "disabled"
    if grant is None:
        return "not_found"
    if UUID(str(grant["admin_user_id"])) != admin_user_id:
        return "wrong_admin"
    if grant["revoked_at"] is not None:
        return "revoked"
    expires_at = grant["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=now.tzinfo)
    if expires_at <= now:
        return "expired"
    return None
