"""共享基础：枚举、topic_key 规范化、canonical hash、HMAC 与 cursor 签名。

对应规格 §4（标识符/时间/幂等）、§5.1（枚举）、§5.2（优先级）、
§18.1（HMAC 域分离）、§19.9（cursor 签名契约）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import secrets
import unicodedata
from typing import Any, Literal

# ---------------------------------------------------------------------------
# 枚举（§5.1）
# ---------------------------------------------------------------------------

ActorType = Literal[
    "user",
    "conversation_agent",
    "activity_agent",
    "knowledge_graph_ui",
    "summary_projection",
    "system",
    "admin",
]

InputKind = Literal["evidence", "command", "projection", "maintenance"]

OperationType = Literal[
    "conversation_evidence",
    "activity_evidence",
    "correct_memory",
    "forget_memory",
    "restore_memory",
    "override_learner_profile",
    "review_candidate",
    "set_graph_state",
    "project_summary_to_graph",
    "rebuild_index",
    "verify_checksums",
    "purge_tombstones",
    "cleanup_orphan_versions",
    "cleanup_checkpoints",
    "purge_account_memory",
]

OperationStatus = Literal[
    "queued",
    "running",
    "retry_wait",
    "succeeded",
    "needs_review",
    "dead_letter",
    "cancelled",
]

TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"succeeded", "needs_review", "dead_letter", "cancelled"}
)

GraphStatus = Literal["learning", "proficient", "expert"]

# ---------------------------------------------------------------------------
# 优先级（§5.2）
# ---------------------------------------------------------------------------

PRIORITY_P0 = 100  # 纠正、删除、恢复、候选审核
PRIORITY_P1 = 80  # 用户知识图谱标记
PRIORITY_P2 = 50  # 对话总结、总结到图谱的派生更新
PRIORITY_P3 = 20  # 用户动态证据
PRIORITY_P4 = 0  # 维护任务

#: operation_type -> (input_kind, priority)。由 payload.kind 推导（§5.3）。
OPERATION_ROUTING: dict[str, tuple[InputKind, int]] = {
    "conversation_evidence": ("evidence", PRIORITY_P2),
    "activity_evidence": ("evidence", PRIORITY_P3),
    "correct_memory": ("command", PRIORITY_P0),
    "forget_memory": ("command", PRIORITY_P0),
    "restore_memory": ("command", PRIORITY_P0),
    "override_learner_profile": ("command", PRIORITY_P0),
    "review_candidate": ("command", PRIORITY_P0),
    "set_graph_state": ("command", PRIORITY_P1),
    "project_summary_to_graph": ("projection", PRIORITY_P2),
    "rebuild_index": ("maintenance", PRIORITY_P4),
    "verify_checksums": ("maintenance", PRIORITY_P4),
    "purge_tombstones": ("maintenance", PRIORITY_P4),
    "cleanup_orphan_versions": ("maintenance", PRIORITY_P4),
    "cleanup_checkpoints": ("maintenance", PRIORITY_P4),
    "purge_account_memory": ("maintenance", PRIORITY_P4),
}

#: 任务级 max_attempts（§11.2）
MAX_ATTEMPTS_BY_PRIORITY: dict[int, int] = {
    PRIORITY_P0: 6,
    PRIORITY_P1: 6,
    PRIORITY_P2: 4,
    PRIORITY_P3: 4,
    PRIORITY_P4: 3,
}

#: 全局 maintenance 固定系统 UUID（§4.1）
SYSTEM_MAINTENANCE_USER_ID = "00000000-0000-0000-0000-000000000000"


def max_attempts_for_priority(priority: int) -> int:
    if priority >= PRIORITY_P0:
        return 6
    if priority >= PRIORITY_P1:
        return 6
    if priority >= PRIORITY_P2:
        return 4
    if priority >= PRIORITY_P3:
        return 4
    return 3


# ---------------------------------------------------------------------------
# trace_id（§4.1）
# ---------------------------------------------------------------------------


def new_trace_id() -> str:
    """无 W3C Trace Context 时生成 32 位十六进制 ID。"""
    return secrets.token_hex(16)


# ---------------------------------------------------------------------------
# topic_key 规范化（§4.3）
# ---------------------------------------------------------------------------

_TOPIC_KEY_MAX_CODEPOINTS = 80
_ALLOWED_CHAR = re.compile(r"[^\w\-]", re.UNICODE)
_CONTROL_OR_FORMAT = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\ufeff]")


class TopicKeyError(ValueError):
    """topic_key 规范化失败（路径穿越、非法字符、长度不足等）。"""


def normalize_topic_title(title: str) -> str:
    """NFKC 规范化 + 去首尾空白（用于展示与匹配）。"""
    normalized = unicodedata.normalize("NFKC", title)
    if _CONTROL_OR_FORMAT.search(normalized):
        raise TopicKeyError("主题名包含控制字符或隐藏字符")
    return normalized.strip()


def topic_key_from_title(title: str) -> str:
    """从规范主题名生成稳定 topic_key。

    规则（§4.3）：NFKC → 空白/标点折叠为单个 '-' → 拉丁小写 →
    仅保留 Unicode 字母、数字和 ASCII '-' → 去连续/首尾 '-' → 1..80 code point。
    """
    normalized = normalize_topic_title(title)
    lowered = normalized.lower()
    out: list[str] = []
    last_dash = False
    for ch in lowered:
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        else:
            # 空白和标点统一折叠成一个 '-'
            if not last_dash and out:
                out.append("-")
                last_dash = True
    key = "".join(out).strip("-")
    key = re.sub(r"-{2,}", "-", key)
    if not key:
        raise TopicKeyError("主题名规范化后为空")
    if "/" in key or "\\" in key or "." in key:
        raise TopicKeyError("topic_key 含路径段字符")
    if len(key) > _TOPIC_KEY_MAX_CODEPOINTS:
        key = key[:_TOPIC_KEY_MAX_CODEPOINTS].rstrip("-")
    if not key:
        raise TopicKeyError("topic_key 长度不足")
    return key


def topic_key_with_conflict_suffix(key: str, normalized_title: str) -> str:
    """不同规范主题产生同一 key 时，追加 '-' + SHA-256 前 8 位（§4.3 第 8 条）。"""
    digest = hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()[:8]
    suffix = f"-{digest}"
    base = key[: _TOPIC_KEY_MAX_CODEPOINTS - len(suffix)].rstrip("-")
    return f"{base}{suffix}"


def validate_existing_topic_key(key: str) -> str:
    """校验已有 topic_key 的合法性（API 路径参数防御）。"""
    if not (1 <= len(key) <= _TOPIC_KEY_MAX_CODEPOINTS):
        raise TopicKeyError("topic_key 长度非法")
    if _CONTROL_OR_FORMAT.search(key):
        raise TopicKeyError("topic_key 含控制字符或隐藏字符")
    if any(seg in key for seg in ("/", "\\", ".")):
        raise TopicKeyError("topic_key 含路径穿越字符")
    if _ALLOWED_CHAR.search(key):
        raise TopicKeyError("topic_key 含非法字符")
    if key != key.strip("-") or "--" in key:
        raise TopicKeyError("topic_key 含连续或首尾 '-'")
    return key


# ---------------------------------------------------------------------------
# RFC 8785 JSON Canonicalization（§4.5）
# ---------------------------------------------------------------------------

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
    """RFC 8785 JSON Canonicalization Scheme 序列化。"""
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


def idempotency_payload_hash(payload: Any) -> str:
    """Pydantic 校验后的公开 payload → JCS → SHA-256 小写十六进制。"""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# HMAC 域分离（§18.1）
# ---------------------------------------------------------------------------


def hmac_hex(key: str | bytes, domain: str, value: str) -> str:
    """HMAC-SHA256(key, "{domain}:{value}")，小写十六进制。"""
    key_bytes = key.encode("utf-8") if isinstance(key, str) else key
    message = f"{domain}:{value}".encode()
    return hmac.new(key_bytes, message, hashlib.sha256).hexdigest()


def user_log_hash(key: str, user_id: str) -> str:
    return hmac_hex(key, "user:v1", user_id)


def user_privacy_hash(key: str, user_id: str) -> str:
    """长期隐私摘要（account_deletion_manifest 等）。"""
    return hmac_hex(key, "privacy-audit:v1", user_id)


def evidence_ref_hash(key: str, ref: str) -> str:
    return hmac_hex(key, "evidence-ref:v1", ref)


def source_ref_hash(key: str, ref: str) -> str:
    return hmac_hex(key, "source-ref:v1", ref)


def cursor_principal_hash(key: str, user_id: str) -> str:
    """cursor 专用短期 principal 摘要，不复用隐私域（§19.9）。"""
    return hmac_hex(key, "cursor-principal:v1", user_id)


# ---------------------------------------------------------------------------
# 不透明 cursor（§19.9）
# ---------------------------------------------------------------------------


def sign_cursor(key: str, payload: dict[str, Any]) -> str:
    """canonical JSON payload → base64url，追加独立 HMAC 签名。"""
    body = canonical_json(payload).encode("utf-8")
    body_b64 = base64.urlsafe_b64encode(body).decode("ascii").rstrip("=")
    signature = hmac_hex(key, "cursor:v1", body_b64)
    return f"{body_b64}.{signature}"


class CursorError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def verify_cursor(
    key: str,
    token: str,
    *,
    route: str,
    principal_hash: str,
    normalized_filters: dict[str, Any],
    now_epoch: float,
) -> dict[str, Any]:
    """验签并绑定路由、主体、筛选器和过期时间。"""
    try:
        body_b64, signature = token.rsplit(".", 1)
    except ValueError as exc:
        raise CursorError("CURSOR_INVALID", "cursor 结构非法") from exc
    expected = hmac_hex(key, "cursor:v1", body_b64)
    if not hmac.compare_digest(expected, signature):
        raise CursorError("CURSOR_INVALID", "cursor 签名不匹配")
    padding = "=" * (-len(body_b64) % 4)
    try:
        import json

        payload: dict[str, Any] = json.loads(
            base64.urlsafe_b64decode(body_b64 + padding).decode("utf-8")
        )
    except Exception as exc:
        raise CursorError("CURSOR_INVALID", "cursor payload 无法解析") from exc
    if payload.get("route") != route:
        raise CursorError("CURSOR_INVALID", "cursor 路由不匹配")
    if payload.get("principal_hash") != principal_hash:
        raise CursorError("CURSOR_INVALID", "cursor 主体不匹配")
    if payload.get("normalized_filters") != normalized_filters:
        raise CursorError("CURSOR_INVALID", "cursor 筛选条件不匹配")
    expires_at = payload.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        raise CursorError("CURSOR_INVALID", "cursor 缺少过期时间")
    if now_epoch > float(expires_at):
        raise CursorError("CURSOR_EXPIRED", "cursor 已过期")
    return payload


# ---------------------------------------------------------------------------
# 候选匹配键与删除抑制键（§8.8 / §8.7）
# ---------------------------------------------------------------------------


def candidate_match_key(
    candidate_type: str, normalized_topic_or_category: str, summary: str
) -> str:
    """拒绝候选后阻止 30 天内重复生成的匹配键。"""
    summary_hash = hashlib.sha256(summary.encode("utf-8")).hexdigest()
    return f"{candidate_type}:{normalized_topic_or_category}:{summary_hash}"[:300]
