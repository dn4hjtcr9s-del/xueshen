"""账号/密码规则与哈希（方案 §4.3）。

- 用户名：3–64 位小写 ASCII 字母/数字/下划线，字母或数字开头；strip + lowercase
  规范化后唯一；保留用户名禁止注册。
- 邮箱：可选，strip + lowercase，部分唯一索引（迁移 0001 已建）。
- 密码：最少 10 字符，至少一个 ASCII 字母 + 一个 ASCII 数字；UTF-8 字节上限 72；
  bcrypt cost=12。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets

import bcrypt

from backend.auth_service.errors import invalid_email, invalid_username, weak_password

#: 规范化后 3–64 位；字母或数字开头；其余仅小写字母/数字/下划线
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]{2,63}$")

#: 保留用户名（方案 §4.3）：禁止注册
RESERVED_USERNAMES = frozenset(
    {
        "admin",
        "administrator",
        "root",
        "system",
        "support",
        "official",
        "gewu",
        "api",
        "www",
    }
)

#: 邮箱最长 320 字符（RFC 上限）；结构校验保持简单（无验证邮件，方案 §13 明确不做）
_EMAIL_RE = re.compile(r"^[^@\s]{1,200}@[^@\s]{1,200}\.[^@\s]{2,60}$")

#: bcrypt 固定 cost（方案 §4.3）
_BCRYPT_ROUNDS = 12

#: 密码 UTF-8 字节上限（bcrypt 截断边界，方案 §4.3）
_PASSWORD_MAX_BYTES = 72

#: 无此账号时仍执行同代价 bcrypt 校验，避免时序侧信道泄露账号是否存在
DUMMY_PASSWORD_HASH = bcrypt.hashpw(
    b"gewu-dummy-password", bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
).decode("ascii")


def normalize_username(raw: str) -> str:
    return raw.strip().lower()


def normalize_email(raw: str) -> str:
    return raw.strip().lower()


def validate_username(raw: str) -> str:
    """规范化并校验用户名；失败抛 AUTH_INVALID_USERNAME。"""
    normalized = normalize_username(raw)
    if not _USERNAME_RE.fullmatch(normalized):
        raise invalid_username("用户名需为 3–64 位小写字母、数字或下划线，且以字母或数字开头")
    if normalized in RESERVED_USERNAMES:
        raise invalid_username("该用户名为保留名称，不可注册")
    return normalized


def validate_email(raw: str) -> str:
    """规范化并校验邮箱；失败抛 AUTH_INVALID_EMAIL。"""
    normalized = normalize_email(raw)
    if len(normalized) > 320 or not _EMAIL_RE.fullmatch(normalized):
        raise invalid_email("邮箱格式不正确")
    return normalized


def validate_password(raw: str) -> None:
    """校验密码策略；失败抛 AUTH_WEAK_PASSWORD（方案 §4.3 / 评审 P2-10）。"""
    if len(raw) < 10:
        raise weak_password("密码至少需要 10 个字符")
    if len(raw.encode("utf-8")) > _PASSWORD_MAX_BYTES:
        raise weak_password(f"密码过长（UTF-8 编码不能超过 {_PASSWORD_MAX_BYTES} 字节）")
    has_letter = any(c.isascii() and c.isalpha() for c in raw)
    # 评审 P2-10：数字同样限定 ASCII 0-9（isdigit() 会接受 Unicode 数字）
    has_digit = any("0" <= c <= "9" for c in raw)
    if not (has_letter and has_digit):
        raise weak_password("密码必须同时包含至少一个 ASCII 字母和一个数字")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode(
        "ascii"
    )


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


# bcrypt cost=12 是 CPU 密集型同步调用（评审 P2-8）：不得直接阻塞事件循环，
# 统一经受控线程池执行（信号量限制并发，避免恶意登录请求打满 CPU）。
_BCRYPT_SEMAPHORE = asyncio.Semaphore(4)


async def hash_password_async(password: str) -> str:
    async with _BCRYPT_SEMAPHORE:
        return await asyncio.to_thread(hash_password, password)


async def verify_password_async(password: str, password_hash: str) -> bool:
    async with _BCRYPT_SEMAPHORE:
        return await asyncio.to_thread(verify_password, password, password_hash)


def new_refresh_token() -> str:
    """不透明 refresh token：32 字节随机 = 256 bit 熵（方案 §4.4）。"""
    return secrets.token_urlsafe(32)


def refresh_token_hash(token: str) -> bytes:
    """数据库只存 SHA-256 哈希（方案 §4.4）。"""
    return hashlib.sha256(token.encode("ascii")).digest()
