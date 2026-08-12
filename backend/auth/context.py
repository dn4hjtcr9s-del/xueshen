"""内部统一认证上下文（规格 §18.1）。

ProductionJwtAuthAdapter 与 DevelopmentAuthAdapter 都输出同一个 AuthContext。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

ActorType = Literal[
    "user",
    "conversation_agent",
    "activity_agent",
    "knowledge_graph_ui",
    "summary_projection",
    "system",
    "admin",
]

# 全部 scope（§18.2）
SCOPE_MEMORY_READ = "memory:read"
SCOPE_MEMORY_SUBMIT_EVIDENCE = "memory:submit_evidence"
SCOPE_MEMORY_CORRECT = "memory:correct"
SCOPE_MEMORY_DELETE = "memory:delete"
SCOPE_MEMORY_RESTORE = "memory:restore"
SCOPE_MEMORY_REVIEW = "memory:review"
SCOPE_MEMORY_CANCEL = "memory:cancel"
SCOPE_MEMORY_GRAPH_STATE = "memory:graph_state"
SCOPE_MEMORY_CONTEXT = "memory:context"
SCOPE_MEMORY_MAINTENANCE = "memory:maintenance"
SCOPE_MEMORY_BREAK_GLASS = "memory:break_glass"

ALL_SCOPES: frozenset[str] = frozenset(
    {
        SCOPE_MEMORY_READ,
        SCOPE_MEMORY_SUBMIT_EVIDENCE,
        SCOPE_MEMORY_CORRECT,
        SCOPE_MEMORY_DELETE,
        SCOPE_MEMORY_RESTORE,
        SCOPE_MEMORY_REVIEW,
        SCOPE_MEMORY_CANCEL,
        SCOPE_MEMORY_GRAPH_STATE,
        SCOPE_MEMORY_CONTEXT,
        SCOPE_MEMORY_MAINTENANCE,
        SCOPE_MEMORY_BREAK_GLASS,
    }
)

# Agent 委托契约（§18.4，评审 #15）：允许持有的 scope 上限
AGENT_ALLOWED_SCOPES: frozenset[str] = frozenset(
    {SCOPE_MEMORY_READ, SCOPE_MEMORY_SUBMIT_EVIDENCE, SCOPE_MEMORY_CONTEXT}
)
AGENT_ACTOR_TYPES: tuple[str, ...] = ("conversation_agent", "activity_agent")

# 本地 dev 身份预设 scopes：普通用户能力（§18.1）
DEV_USER_DEFAULT_SCOPES: frozenset[str] = frozenset(
    {
        SCOPE_MEMORY_READ,
        SCOPE_MEMORY_SUBMIT_EVIDENCE,
        SCOPE_MEMORY_CORRECT,
        SCOPE_MEMORY_DELETE,
        SCOPE_MEMORY_RESTORE,
        SCOPE_MEMORY_REVIEW,
        SCOPE_MEMORY_CANCEL,
        SCOPE_MEMORY_GRAPH_STATE,
        SCOPE_MEMORY_CONTEXT,
    }
)


@dataclass(frozen=True)
class AuthContext:
    """认证通过后的内部身份。浏览器不得自行注入其中任何字段。"""

    user_id: UUID
    actor_type: ActorType
    scopes: frozenset[str] = field(default_factory=frozenset)
    issuer: str | None = None
    external_subject: str | None = None
    # Agent 委托契约（§18.4，评审 #15）：服务主体 sub；此时 user_id 为委托用户
    actor_principal: str | None = None
    break_glass_grant_id: UUID | None = None

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes
