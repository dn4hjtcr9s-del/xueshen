"""Access token 签发（方案 §4.4 / §6.2）。

- RS256 非对称签名；私钥文件仅认证服务进程持有（AUTH_PRIVATE_KEY_FILE）。
- claims 完整：iss / aud / sub / actor_type / scopes / iat / exp / jti，缺一即被
  verifier 拒绝（backend/auth/verifier.py 严格契约）。
- 有效期硬上限 300 秒（verifier auth_token_max_lifetime_seconds）。
"""

from __future__ import annotations

import time
from collections.abc import Collection
from pathlib import Path
from uuid import UUID, uuid4

import jwt

from backend.auth.context import DEV_USER_DEFAULT_SCOPES
from backend.settings import Settings

#: 普通用户 access token 的默认 scopes（方案 §4.4，与 dev 用户能力一致）
USER_DEFAULT_SCOPES: frozenset[str] = frozenset(DEV_USER_DEFAULT_SCOPES)

#: 缺省 issuer（方案 §6.2：AUTH_ISSUER=gewu-auth 固定值）
DEFAULT_ISSUER = "gewu-auth"

#: access token 有效期（秒）；不得超过 verifier 硬上限
ACCESS_TOKEN_LIFETIME_SECONDS = 300


class AccessTokenIssuer:
    """加载私钥并签发符合 verifier 严格契约的 RS256 JWT。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._private_key: str | None = None

    def _load_private_key(self) -> str:
        """首次签发时加载私钥文件；缺失/不可读时抛出明确错误。"""
        if self._private_key is not None:
            return self._private_key
        key_file = self._settings.auth_private_key_file
        if not key_file:
            raise RuntimeError("AUTH_PRIVATE_KEY_FILE 未配置，无法签发 access token")
        try:
            self._private_key = Path(key_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"私钥文件读取失败: {type(exc).__name__}") from exc
        return self._private_key

    @property
    def issuer(self) -> str:
        return self._settings.auth_issuer or DEFAULT_ISSUER

    @property
    def audience(self) -> str:
        return self._settings.auth_audience

    def issue(
        self,
        *,
        user_id: UUID,
        scopes: Collection[str] = USER_DEFAULT_SCOPES,
        actor_type: str = "user",
        lifetime_seconds: int = ACCESS_TOKEN_LIFETIME_SECONDS,
    ) -> str:
        """签发短时 access token；返回 JWT 字符串。"""
        now = int(time.time())
        claims = {
            "iss": self.issuer,
            "aud": self.audience,
            "sub": str(user_id),
            "actor_type": actor_type,
            "scopes": sorted(scopes),
            "iat": now,
            "exp": now + lifetime_seconds,
            "jti": str(uuid4()),
        }
        return jwt.encode(claims, self._load_private_key(), algorithm="RS256")
