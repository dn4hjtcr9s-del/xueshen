"""查询返回结构（规格 §7 / §12.3 / §19.4 / §19.6 / memory-rebuild §2.4 D1/D3）。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Generic, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from backend.memory.contracts.commands import CandidateContentView

T = TypeVar("T")

#: 记忆工具 query 单项长度上限（§2.4 D3：判别性检索词，不是自然语言长句）
MEMORY_TOOL_QUERY_MAX_CHARS = 200
#: 记忆工具单次 search 的 query 条数上限，与 sqlalchemy 参数数量、模型一次给出的检索词数量匹配
MEMORY_TOOL_MAX_QUERIES = 8


class CursorPage(BaseModel, Generic[T]):  # noqa: UP046 保留规格 §19.4 原始 Generic 形式
    items: list[T]
    next_cursor: str | None
    has_more: bool


class LearnerMemoryView(BaseModel):
    memory_type: Literal["learner"] = "learner"
    memory_id: Literal["learner"] = "learner"
    version: int = Field(ge=1)
    preferences: list[str]
    goals: list[str]
    plans: list[str]
    evidence_refs: list[str] = Field(max_length=100)
    confidence: float | None = Field(default=None, ge=0, le=1)
    updated_at: datetime


class MasteryMemoryView(BaseModel):
    memory_type: Literal["mastery"] = "mastery"
    memory_id: str = Field(pattern=r"^mastery:.+")
    topic_key: str = Field(min_length=1, max_length=160)
    topic_title: str = Field(min_length=1, max_length=240)
    version: int = Field(ge=1)
    overview: str
    understood: list[str]
    difficulties: list[str]
    review_advice: list[str]
    evidence_refs: list[str] = Field(max_length=100)
    confidence: float | None = Field(default=None, ge=0, le=1)
    updated_at: datetime


#: memory_type 作为 Pydantic discriminator（v1.1 裁决 2）
MemoryDocumentView = Annotated[
    LearnerMemoryView | MasteryMemoryView,
    Field(discriminator="memory_type"),
]


class MemoryIndexEntryView(BaseModel):
    memory_id: str
    memory_type: Literal["learner", "mastery"]
    topic_key: str | None
    title: str
    version: int
    updated_at: datetime


class MemoryIndexView(BaseModel):
    version: int = Field(ge=0)
    entries: list[MemoryIndexEntryView]
    updated_at: datetime | None
    stale: bool


class ReviewCandidateView(BaseModel):
    candidate_id: UUID
    candidate_type: Literal["learner", "mastery", "topic_conflict", "version_conflict"]
    base_memory_id: str | None
    base_version: int | None
    topic_key: str | None
    candidate_content: CandidateContentView
    evidence_refs: list[str] = Field(max_length=100)
    confidence: float = Field(ge=0, le=1)
    status: Literal["pending", "accepted", "corrected", "rejected", "expired"]
    resolution_target: Literal["merge_existing", "create_new_topic"] | None
    target_memory_id: str | None
    resolved_operation_id: UUID | None
    reviewed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DeletedMemoryItem(BaseModel):
    memory_id: str
    memory_type: Literal["learner", "mastery"]
    topic_key: str | None
    title: str
    deleted_version: int
    deleted_at: datetime
    restore_until: datetime


class MemorySearchHit(BaseModel):
    memory_id: str
    memory_type: Literal["learner", "mastery"]
    topic_key: str | None
    title: str
    summary: str
    matched_excerpt: str | None
    evidence_refs: list[str] = Field(max_length=100)
    version: int
    updated_at: datetime
    confidence: float | None
    score: float


class MemorySearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=500)
    topic_keys: list[str] = Field(default_factory=list, max_length=20)
    memory_types: list[Literal["learner", "mastery"]] = Field(default_factory=list)
    cursor: str | None = Field(default=None, max_length=1000)
    limit: int = Field(default=10, ge=1, le=50)


class MemoryNotification(BaseModel):
    notification_id: UUID
    event_type: str
    title: str
    body: str
    aggregate_type: str
    aggregate_id: str
    read_at: datetime | None
    created_at: datetime


class MemoryNotificationPage(CursorPage[MemoryNotification]):
    unread_count: int = Field(ge=0)


# ---------------------------------------------------------------------------
# 记忆工具（memory-rebuild §2.4 D1/D3 / §5.7 Phase 5）
#
# 三个内部端点：search 定位（纯关键词、不含正文）→ read 下沉（版本化正文分段）
# → prime 首轮注入（summary + index 注册表目录）。user_id 一律取自认证上下文，
# 请求体不接受 user_id（extra="forbid" 会拒绝）。
# ---------------------------------------------------------------------------


class MemoryToolSearchRequest(BaseModel):
    """`memory.search` 入参（§2.4 D3①）。"""

    model_config = ConfigDict(extra="forbid")

    queries: list[Annotated[str, Field(min_length=1, max_length=MEMORY_TOOL_QUERY_MAX_CHARS)]] = (
        Field(min_length=1, max_length=MEMORY_TOOL_MAX_QUERIES)
    )
    #: any = 命中任一 query 即入选；all = 每条候选必须命中全部 query
    match_mode: Literal["any", "all"] = "any"
    max_results: int = Field(default=10, ge=1, le=50)


class MemoryToolSearchItem(BaseModel):
    """检索命中的注册表条目：**不含正文**，强制 search 定位 → read 下沉两段式。"""

    memory_id: str
    #: 注册表 name（PG 列名 title，§3.4 决策：同一份投影不开两列）
    name: str
    #: 注册表 description（PG 列名 summary）
    description: str
    keywords: list[str]
    version: int
    updated_at: datetime


class MemoryToolSearchResponse(BaseModel):
    items: list[MemoryToolSearchItem]
    #: 命中数达到 max_results 且仍有更多命中
    truncated: bool


class MemoryToolReadRequest(BaseModel):
    """`memory.read` 入参（§2.4 D3②）：按行分段读取。"""

    model_config = ConfigDict(extra="forbid")

    memory_id: str = Field(min_length=1, max_length=160)
    line_offset: int = Field(default=0, ge=0)
    max_lines: int = Field(default=200, ge=1, le=500)


class MemoryToolReadResponse(BaseModel):
    """正文片段 + 版本/checksum 溯源（§5.7：引用可回查到具体 document version）。"""

    memory_id: str
    version: int
    #: 活动版本整篇正文的 SHA-256（不是返回片段的摘要）
    checksum: str
    content: str
    line_offset: int
    total_lines: int
    #: 后面还有行没有返回
    truncated: bool


class MemoryToolPrimeRequest(BaseModel):
    """`memory.prime` 入参：空对象（§2.4 D1 首轮注入，用户由令牌决定）。"""

    model_config = ConfigDict(extra="forbid")


class MemoryToolPrimeIndexEntry(BaseModel):
    """首轮注入的目录提示条目：memory_id / name / description / keywords，不含正文。"""

    memory_id: str
    name: str
    description: str
    keywords: list[str]


class MemoryToolPrimeResponse(BaseModel):
    """首轮 prime：`memory_summary.md` 摘要 + 注册表目录（§2.4 D1）。

    summary 缺失/损坏/超限时不报错：返回空 summary 并置 ``degraded=true``
    （§5.7：按现有降级策略返回空 prime/旧摘要，并记录 memory_prime_degraded）。
    """

    summary: str
    #: summary 首行标记（§2.3：首行必须恰好是 v1）；缺失时回落默认 "v1"
    schema_version: str
    #: 生成时间元信息；无权威时间戳时为 None
    generated_at: datetime | None
    index_entries: list[MemoryToolPrimeIndexEntry]
    #: summary 超过独立小预算被截断
    summary_truncated: bool
    degraded: bool
