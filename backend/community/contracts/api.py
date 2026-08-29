"""Community 公共 API DTO（方案 §6.6 v1.5 冻结 + §19 D45/D47 增补）。

规则（§6.6）：所有 DTO 不持有内部 user_id/email/JWT subject；作者操作只依赖
viewer_is_author；hidden 不通过公共 DTO 暴露，读取表现为 COMMUNITY_NOT_FOUND。
时间字段为 ISO8601 UTC（pydantic datetime 默认序列化，D47）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class Page[T](BaseModel):
    """通用分页信封（§19.45 冻结：{items, next_cursor, has_more}）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[T]
    next_cursor: str | None
    has_more: bool


class CommunityBoard(BaseModel):
    """板块视图（§6.6）。"""

    model_config = ConfigDict(extra="forbid")

    board_id: UUID
    slug: str
    name: str
    description: str


class CommunityAuthor(BaseModel):
    """公开作者视图：只含展示名（§9.1：不返回内部身份/邮箱）。"""

    model_config = ConfigDict(extra="forbid")

    display_name: str


class CommunityPostSummary(BaseModel):
    """帖子列表项（§6.6：Summary 恒非 null title，列表只含 active）。"""

    model_config = ConfigDict(extra="forbid")

    post_id: UUID
    board: CommunityBoard
    author: CommunityAuthor
    title: str
    pinned: bool
    solved: bool
    reply_count: int
    like_count: int
    viewer_liked: bool
    attachments: list[CommunityAttachment]
    created_at: datetime
    last_activity_at: datetime


class CommunityPostDetail(CommunityPostSummary):
    """帖子详情（§6.6：deleted 墓碑 title/body=null、deleted=true）。

    Summary 的 title 恒非 null（列表只含 active），此处覆盖为可空以支持墓碑。
    """

    title: str | None  # type: ignore[assignment]  # §6.6 墓碑契约：deleted 时为 null
    body: str | None
    deleted: bool
    discussion_status: Literal["open", "closed"]
    viewer_is_author: bool
    solved_reply_id: UUID | None
    deleted_at: datetime | None


class CommunityReplyView(BaseModel):
    """回复视图（§6.6：deleted 回复 body=null 占位行保留讨论结构）。"""

    model_config = ConfigDict(extra="forbid")

    reply_id: UUID
    author: CommunityAuthor
    body: str | None
    deleted: bool
    viewer_is_author: bool
    solved: bool
    created_at: datetime


class CommunityPostDetailResponse(BaseModel):
    """帖子详情响应（§8.4）：帖子 + 一页回复。"""

    model_config = ConfigDict(extra="forbid")

    post: CommunityPostDetail
    replies: Page[CommunityReplyView]


class CommunityNotification(BaseModel):
    """社区通知视图（§6.6：不返回 actor_user_id/recipient_user_id）。"""

    model_config = ConfigDict(extra="forbid")

    notification_id: UUID
    event_type: Literal[
        "post_replied",
        "reply_marked_solved",
        "application_approved",
        "application_rejected",
    ]
    title: str
    body: str
    read_at: datetime | None
    created_at: datetime
    post_id: UUID | None
    reply_id: UUID | None
    board_slug: str | None


class CommunityNotificationPage(BaseModel):
    """通知分页响应（§8.6/D45：额外顶层 unread_count）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[CommunityNotification]
    next_cursor: str | None
    has_more: bool
    unread_count: int


# ---------------------------------------------------------------------------
# 写请求（PR-C 使用，冻结在此避免契约漂移）
# ---------------------------------------------------------------------------


class CommunityAttachment(BaseModel):
    """附件视图（§八）。"""

    model_config = ConfigDict(extra="forbid")

    attachment_id: UUID
    url: str
    width: int
    height: int
    mime: str
    position: int


class CommunityAttachmentSummary(BaseModel):
    """上传成功后返回的附件元信息。"""

    model_config = ConfigDict(extra="forbid")

    attachment_id: UUID
    url: str
    mime: str
    width: int
    height: int
    size_bytes: int


class CreatePostRequest(BaseModel):
    """创建帖子请求（§8.3）：不含 user_id（§9.1 服务端取认证上下文）。"""

    model_config = ConfigDict(extra="forbid")

    board_id: UUID
    title: str
    body: str
    attachment_ids: list[UUID] | None = None


class CreateReplyRequest(BaseModel):
    """创建回复请求（§8.4）。"""

    model_config = ConfigDict(extra="forbid")

    body: str


class ResolveRequest(BaseModel):
    """标记解决/取消解决请求（§8.5：reply_id=null 表示取消解决）。"""

    model_config = ConfigDict(extra="forbid")

    reply_id: UUID | None


class BoardApplicationRequest(BaseModel):
    """申请建吧请求（§八）。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    slug: str
    description: str
    reason: str


class BoardApplicationView(BaseModel):
    """建吧申请视图（§八 D44：不含 reviewer_id）。"""

    model_config = ConfigDict(extra="forbid")

    application_id: UUID
    name: str
    slug: str
    description: str
    reason: str
    status: Literal["pending", "approved", "rejected"]
    board_id: UUID | None
    reviewed_at: datetime | None
    reject_reason: str | None
    created_at: datetime


class RejectApplicationRequest(BaseModel):
    """拒绝建吧申请请求。"""

    model_config = ConfigDict(extra="forbid")

    reason: str


class PermissionsResponse(BaseModel):
    """权限信息（§八）。"""

    model_config = ConfigDict(extra="forbid")

    is_community_admin: bool


class BoardDetailResponse(BaseModel):
    """板块详情响应（§八 #2）。"""

    model_config = ConfigDict(extra="forbid")

    board_id: UUID
    slug: str
    name: str
    description: str
    post_count: int
    created_at: datetime
    viewer_is_owner: bool


class BoardListResponse(BaseModel):
    """板块列表响应（§八 #1）。"""

    model_config = ConfigDict(extra="forbid")

    items: list[BoardDetailResponse]
