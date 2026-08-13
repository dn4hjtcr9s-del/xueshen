"""Conversation API 共享依赖（方案 §17 / §19.9 cursor）。

cursor：服务端 HMAC 签名的不透明 cursor（复用 memory 域 cursor 助手模式）；
前端不得解析或自行构造（§17.1）。限流：Turn 创建默认 10 次/分钟/用户
（§17.2 / Q13，复用 FixedWindowRateLimiter 第一版实现）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.services.conversation_service import ConversationService
from backend.settings import Settings


class ConversationApiContext:
    """API 进程依赖组合体（composition root 装配）。"""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        service: ConversationService,
        rate_limiter: Any,
    ) -> None:
        self.settings = settings
        self.session_factory = session_factory
        self.service = service
        self.rate_limiter = rate_limiter

    # ---- cursor（§17.1：HMAC 签名不透明 cursor） ----
    def encode_cursor(self, updated_at: datetime, thread_id: UUID) -> str:
        import base64
        import hashlib
        import hmac

        raw = f"{updated_at.isoformat()}|{thread_id}".encode()
        digest = hmac.new(self.settings.cursor_hmac_key.encode(), raw, hashlib.sha256).hexdigest()[
            :32
        ]
        return base64.urlsafe_b64encode(f"{digest}|{raw.decode()}".encode()).decode()

    def decode_cursor(self, cursor: str) -> tuple[datetime, UUID] | None:
        import base64
        import hashlib
        import hmac

        try:
            decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
            digest, raw = decoded.split("|", 1)
            expected = hmac.new(
                self.settings.cursor_hmac_key.encode(), raw.encode(), hashlib.sha256
            ).hexdigest()[:32]
            if digest != expected:
                return None
            updated_at_str, thread_id_str = raw.split("|", 1)
            return datetime.fromisoformat(updated_at_str), UUID(thread_id_str)
        except Exception:
            return None


def build_conversation_api_context(
    *,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    service: ConversationService,
    rate_limiter: Any,
) -> ConversationApiContext:
    """composition root 装配 API 上下文。"""
    return ConversationApiContext(
        settings=settings,
        session_factory=session_factory,
        service=service,
        rate_limiter=rate_limiter,
    )


def get_conversation_context(request: Request) -> ConversationApiContext:
    """FastAPI 依赖：从 app.state 取 Conversation API 上下文。"""
    ctx = getattr(request.app.state, "conversation_api_context", None)
    if ctx is None:
        raise RuntimeError("Conversation API 上下文未装配")
    return ctx  # type: ignore[no-any-return]
