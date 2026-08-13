"""Agent 委托 token 签发（方案 §8 / §18.4）。

- 契约函数 issue_agent_token：仅供同一受信后端进程内调用（agent 与认证服务
  同进程，方案裁决），不向浏览器暴露，不新增公开 HTTP 接口。
- 越界 scope 直接抛错（不静默裁剪）；claims 带 delegated_sub 供 verifier
  解析回委托用户的内部 user_id（memory 侧业务代码零改动）。
"""

from __future__ import annotations

import time
from collections.abc import Collection
from typing import Literal
from uuid import uuid4

import jwt

from backend.auth.context import AGENT_ALLOWED_SCOPES
from backend.auth_service.tokens import ACCESS_TOKEN_LIFETIME_SECONDS, AccessTokenIssuer
from backend.settings import get_settings


def issue_agent_token(
    *,
    agent_subject: str,  # agent 服务主体，如 "conversation-agent-prod-01"
    delegated_sub: str,  # 委托用户的 sub（= user_id 字符串）
    actor_type: Literal["conversation_agent", "activity_agent"],
    requested_scopes: Collection[str],  # 超出 AGENT_ALLOWED_SCOPES 直接抛错，不静默裁剪
    lifetime_seconds: int = ACCESS_TOKEN_LIFETIME_SECONDS,
) -> str:
    """为同进程 agent 签发委托 token（方案 §8 契约）。"""
    issuer = AccessTokenIssuer(get_settings())
    unknown = set(requested_scopes) - AGENT_ALLOWED_SCOPES
    if unknown:
        raise ValueError(f"Agent scope 越界: {sorted(unknown)}")
    now = int(time.time())
    claims = {
        "iss": issuer.issuer,
        "aud": issuer.audience,
        "sub": agent_subject,
        "actor_type": actor_type,
        "scopes": sorted(requested_scopes),
        "delegated_sub": delegated_sub,
        "iat": now,
        "exp": now + lifetime_seconds,
        "jti": str(uuid4()),
    }
    private_key = _private_key(issuer)
    return jwt.encode(claims, private_key, algorithm="RS256")


def _private_key(issuer: AccessTokenIssuer) -> str:
    """复用 AccessTokenIssuer 的私钥加载（模块级函数保持契约签名不变）。"""
    return issuer._load_private_key()  # 同模块内部复用，避免重复 IO
