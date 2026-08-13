"""Access token 签发（方案 §4.4 / §6.2）。

- RS256 非对称签名；私钥文件仅认证服务进程持有（AUTH_PRIVATE_KEY_FILE）。
- claims 完整：iss / aud / sub / actor_type / scopes / iat / exp / jti，缺一即被
  verifier 拒绝（backend/auth/verifier.py 严格契约）。
- 有效期硬上限 300 秒（verifier auth_token_max_lifetime_seconds）。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Collection
from pathlib import Path
from uuid import UUID, uuid4

import jwt

from backend.auth.context import (
    ACTOR_TYPES,
    DEFAULT_AUTH_ISSUER,
    DEV_USER_DEFAULT_SCOPES,
)
from backend.settings import Settings

#: 普通用户 access token 的默认 scopes（方案 §4.4，与 dev 用户能力一致）
USER_DEFAULT_SCOPES: frozenset[str] = frozenset(DEV_USER_DEFAULT_SCOPES)

#: 缺省 issuer（与 verifier 验签端共用同一 fallback，复审 P3）
DEFAULT_ISSUER = DEFAULT_AUTH_ISSUER

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


class AccessTokenVerifier:
    """验签本服务签发的 access token（/me 用）；不查身份映射。

    支持本地公钥（文件优先，PEM 文本备选）与 AUTH_JWKS_URL（评审 P1-4：
    与 memory verifier 的配置组合保持一致，/me 不再因 JWKS-only 配置失败）。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def verify_sub(self, token: str) -> UUID:
        """验签并返回 sub；失败抛 jwt.PyJWTError（/me 统一映射为 401）。

        复审 P3：与 memory verifier 的严格契约对齐——必需 claims 全集、
        actor_type 白名单、scopes 类型校验、300 秒最长有效期；
        sub 非合法 UUID 视为无效 token（不再抛 ValueError 导致 500）。
        """
        key_file = self._settings.auth_public_key_file
        if key_file:
            try:
                decode_key: str = await asyncio.to_thread(
                    Path(key_file).read_text, encoding="utf-8"
                )
            except OSError as exc:
                raise RuntimeError(f"公钥文件读取失败: {type(exc).__name__}") from exc
        elif self._settings.auth_public_key:
            decode_key = self._settings.auth_public_key
        elif self._settings.auth_jwks_url:
            client = jwt.PyJWKClient(self._settings.auth_jwks_url)
            key_obj = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
            decode_key = key_obj.key
        else:
            raise RuntimeError("认证公钥未配置")
        claims = await asyncio.to_thread(
            jwt.decode,
            token,
            decode_key,
            algorithms=["RS256", "ES256"],
            audience=self._settings.auth_audience,
            issuer=self._settings.auth_issuer or DEFAULT_ISSUER,
            options={"require": ["iss", "aud", "sub", "iat", "exp", "jti", "actor_type", "scopes"]},
        )
        # 严格 claims schema（与 verifier.py 对齐，复审 P3）
        actor_raw = claims["actor_type"]
        if not isinstance(actor_raw, str) or actor_raw not in ACTOR_TYPES:
            raise jwt.InvalidTokenError("actor_type claim 非法")
        scopes_raw = claims["scopes"]
        if not isinstance(scopes_raw, list) or not all(isinstance(s, str) for s in scopes_raw):
            raise jwt.InvalidTokenError("scopes claim 必须是字符串数组")
        iat = int(claims["iat"])
        exp = int(claims["exp"])
        if exp - iat > self._settings.auth_token_max_lifetime_seconds:
            raise jwt.InvalidTokenError("token 有效期超过上限")
        raw_sub = str(claims["sub"])
        try:
            return UUID(raw_sub)
        except ValueError as exc:
            raise jwt.InvalidTokenError("sub 不是合法 UUID") from exc
