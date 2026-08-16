"""内部统一认证上下文（规格 §18.1）。

ProductionJwtAuthAdapter 与 DevelopmentAuthAdapter 都输出同一个 AuthContext。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, get_args
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

#: 合法 actor_type 集合：由 ActorType Literal 派生（复审 Optional：单一定义，
#: 新增 actor 时白名单自动跟随，杜绝漏改）
ACTOR_TYPES: tuple[str, ...] = get_args(ActorType)

#: 缺省 issuer（方案 §6.2：AUTH_ISSUER=gewu-auth 固定值；签发与验签两端共用）
DEFAULT_AUTH_ISSUER = "gewu-auth"

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
# Conversation 域内部 scope（方案 §1.2 / §8.2 / §8.6 / §22）：
# 两个独立 system principal，不授予普通用户与 delegated Agent
SCOPE_CONVERSATION_SOURCE_READ = "conversation:source_read"
SCOPE_MEMORY_SOURCE_DELETE = "memory:source_delete"
# Community 域内部 scope（方案 community §10.3/§13.3，v1.6 冻结）：
# 独立 system principal（D36），加入 ALL_SCOPES 但不加入 AGENT_ALLOWED_SCOPES
SCOPE_COMMUNITY_SOURCE_READ = "community:source_read"
SCOPE_COMMUNITY_ACCOUNT_PURGE = "community:account_purge"
# Study 域内部 scope（方案 study §12.8/D19）：独立 system principal，
# 加入 ALL_SCOPES 但不加入 AGENT_ALLOWED_SCOPES、不授予普通用户
SCOPE_STUDY_ACCOUNT_PURGE = "study:account_purge"

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
        SCOPE_CONVERSATION_SOURCE_READ,
        SCOPE_MEMORY_SOURCE_DELETE,
        SCOPE_COMMUNITY_SOURCE_READ,
        SCOPE_COMMUNITY_ACCOUNT_PURGE,
        SCOPE_STUDY_ACCOUNT_PURGE,
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
