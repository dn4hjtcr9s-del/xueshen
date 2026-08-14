"""跨域共享：不透明 HMAC cursor 与确定性 JSON（方案 §19.9 / D13 / D24 / D40）。

从 backend/memory/contracts/common.py 提取，供 Memory / Conversation / Community
共同使用；Memory 侧 contracts/common.py 保留 re-export 以兼容既有引用点。

v1.6 扩展（D13）：issue_cursor/resolve_cursor 增加可选 principal binding。
- bind_principal=True（默认）：payload 写入 principal_hash，验签要求匹配；
- bind_principal=False：payload 不写 principal_hash，验签要求该字段不存在，
  供 Community 公共帖子列表/详情回复分页等与查看者无关的分页位置使用。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import time
from typing import Any
from uuid import UUID

from backend.settings import Settings


def _hmac_hex(key: str | bytes, domain: str, value: str) -> str:
    """HMAC-SHA256(key, "{domain}:{value}")，小写十六进制（域分离，§18.1）。"""
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    message = f"{domain}:{value}".encode()
    return hmac.new(key_bytes, message, hashlib.sha256).hexdigest()


def cursor_principal_hash(key: str, user_id: str) -> str:
    """cursor 专用短期 principal 摘要，不复用隐私域（§19.9）。"""
    return _hmac_hex(key, "cursor-principal:v1", user_id)


#: RFC 8785 标准转义表：与 memory/contracts/common.py 原实现逐字节一致，
#: 改动会破坏既有游标签名兼容性。
_JCS_ESCAPES = {
    '"': '\\"',
    "\\": "\\\\",
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _jcs_string(value: str) -> str:
    """RFC 8785 JCS 字符串转义（与 common.py 原实现一致）。"""
    parts = ['"']
    for ch in value:
        if ch in _JCS_ESCAPES:
            parts.append(_JCS_ESCAPES[ch])
        elif ord(ch) < 0x20:
            parts.append(f"\\u{ord(ch):04x}")
        else:
            parts.append(ch)
    parts.append('"')
    return "".join(parts)


def _jcs_number(value: float) -> str:
    """RFC 8785 JCS 数字规范化（与 common.py 原实现一致）。"""
    if not math.isfinite(value):
        raise ValueError("JCS 不支持非有限浮点数")
    if value == int(value) and abs(value) < 1e21:
        return str(int(value))
    text = repr(value)
    if "e" in text or "E" in text:
        mantissa, _, exponent = text.lower().partition("e")
        sign = ""
        if exponent.startswith("+"):
            exponent = exponent[1:]
        elif exponent.startswith("-"):
            sign = "-"
            exponent = exponent[1:]
        exponent = exponent.lstrip("0") or "0"
        text = f"{mantissa}e{sign}{exponent}"
    return text


def canonical_json(value: Any) -> str:
    """RFC 8785 JSON Canonicalization Scheme 序列化（D40：幂等哈希依赖）。"""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _jcs_number(value)
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(item) for item in value) + "]"
    if isinstance(value, dict):
        items = sorted(value.items(), key=lambda kv: [ord(c) for c in str(kv[0])])
        return "{" + ",".join(f"{_jcs_string(str(k))}:{canonical_json(v)}" for k, v in items) + "}"
    raise TypeError(f"JCS 不支持的类型: {type(value).__name__}")


def sign_cursor(key: str, payload: dict[str, Any]) -> str:
    """canonical JSON payload → base64url，追加独立 HMAC 签名。"""
    body = canonical_json(payload).encode("utf-8")
    body_b64 = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    signature = _hmac_hex(key, "cursor:v1", body_b64)
    return f"{body_b64}.{signature}"


class CursorError(ValueError):
    """通用游标错误（各域自行映射到自己的公共错误码）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def verify_cursor(
    key: str,
    token: str,
    *,
    route: str,
    principal_hash: str | None,
    normalized_filters: dict[str, Any],
    now_epoch: float,
) -> dict[str, Any]:
    """验签并绑定路由、主体（可选）、筛选器和过期时间。

    principal_hash 为 None 表示该路由的游标不绑定用户（D13 公共游标）：
    验签时要求 payload 中不存在 principal_hash 字段。
    """
    try:
        body_b64, signature = token.rsplit(".", 1)
    except ValueError as exc:
        raise CursorError("CURSOR_INVALID", "cursor 结构非法") from exc
    expected = _hmac_hex(key, "cursor:v1", body_b64)
    if not hmac.compare_digest(expected, signature):
        raise CursorError("CURSOR_INVALID", "cursor 签名不匹配")
    padding = "=" * (-len(body_b64) % 4)
    try:
        payload: dict[str, Any] = json.loads(
            base64.urlsafe_b64decode(body_b64 + padding).decode("utf-8")
        )
    except Exception as exc:
        raise CursorError("CURSOR_INVALID", "cursor payload 无法解析") from exc
    if payload.get("route") != route:
        raise CursorError("CURSOR_INVALID", "cursor 路由不匹配")
    if principal_hash is not None:
        if payload.get("principal_hash") != principal_hash:
            raise CursorError("CURSOR_INVALID", "cursor 主体不匹配")
    elif "principal_hash" in payload:
        raise CursorError("CURSOR_INVALID", "cursor 不应绑定主体")
    if payload.get("normalized_filters") != normalized_filters:
        raise CursorError("CURSOR_INVALID", "cursor 筛选条件不匹配")
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        raise CursorError("CURSOR_INVALID", "cursor 缺少过期时间")
    if now_epoch > float(expires_at):
        raise CursorError("CURSOR_EXPIRED", "cursor 已过期")
    return payload


def issue_cursor(
    settings: Settings,
    *,
    route: str,
    user_id: UUID,
    filters: dict[str, Any],
    sort_key: list[Any],
    bind_principal: bool = True,
) -> str:
    """签发不透明游标（D13：bind_principal=False 时不写入 principal_hash）。"""
    payload: dict[str, Any] = {
        "cursor_version": 1,
        "route": route,
        "normalized_filters": filters,
        "sort_key": sort_key,
        "expires_at": time.time() + settings.cursor_ttl_seconds,
    }
    if bind_principal:
        payload["principal_hash"] = cursor_principal_hash(settings.cursor_hmac_key, str(user_id))
    return sign_cursor(settings.cursor_hmac_key, payload)


def resolve_cursor(
    settings: Settings,
    token: str,
    *,
    route: str,
    user_id: UUID,
    filters: dict[str, Any],
    bind_principal: bool = True,
) -> dict[str, Any]:
    """验签并绑定路由/主体/筛选/过期（D13：公共游标传 bind_principal=False）。"""
    principal = None
    if bind_principal:
        principal = cursor_principal_hash(settings.cursor_hmac_key, str(user_id))
    return verify_cursor(
        settings.cursor_hmac_key,
        token,
        route=route,
        principal_hash=principal,
        normalized_filters=filters,
        now_epoch=time.time(),
    )
