"""Community 域领域常量与状态（方案 §7，v1.6 冻结）。

- COMMUNITY_UUID_NAMESPACE：Community 域统一 UUIDv5 namespace（§11.2），
  板块 seed（§7.1 名称 community-board:{slug}）、source deletion 幂等键等共用；
- 板块 seed 常量与迁移共用同一来源，测试夹具复用。
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid5

#: Community 域统一 namespace（§7.1/§11.2 冻结，迁移与测试夹具复用）
COMMUNITY_UUID_NAMESPACE = UUID("8f0db4c4-0b5c-4f6d-a2b3-c86ef29a8d4a")


class BoardStatus(StrEnum):
    ACTIVE = "active"
    HIDDEN = "hidden"


class PostStatus(StrEnum):
    ACTIVE = "active"
    HIDDEN = "hidden"
    DELETED = "deleted"


class DiscussionStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class ReplyStatus(StrEnum):
    ACTIVE = "active"
    HIDDEN = "hidden"
    DELETED = "deleted"


class OutboxEventType(StrEnum):
    POST_CREATED = "community.post_created"
    REPLY_CREATED = "community.reply_created"
    SOURCE_DELETED = "community.source_deleted"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DELIVERED = "delivered"
    RETRY_WAIT = "retry_wait"
    DEAD_LETTER = "dead_letter"


class OutboxDeliveryResult(StrEnum):
    PUBLISHED = "published"
    SKIPPED_SOURCE_DELETED = "skipped_source_deleted"


class NotificationEventType(StrEnum):
    POST_REPLIED = "post_replied"
    REPLY_MARKED_SOLVED = "reply_marked_solved"


#: 板块 seed（§7.1 冻结：固定 UUID + 文案，迁移/测试/契约共用）
BoardSeed = tuple[str, str, str, str, int]
BOARDS_SEED: tuple[BoardSeed, ...] = (
    (
        "da38ecb6-6f37-5724-be95-10e496b5f3dd",
        "linear-algebra",
        "线性代数",
        "矩阵、向量空间、特征值与线性变换",
        10,
    ),
    ("dcd2a3a5-7e06-5b7e-891f-e065765dcde0", "calculus", "微积分", "极限、导数、积分与级数", 20),
    (
        "d6559df9-da74-51ca-9526-a77229c19237",
        "probability",
        "概率论",
        "概率模型、随机变量与统计推断",
        30,
    ),
    (
        "768737cb-a6a8-527d-a7f1-153bb8841872",
        "study-methods",
        "学习方法",
        "学习方法、复习策略与学习习惯交流",
        40,
    ),
)


def board_id_for_slug(slug: str) -> UUID:
    """由 slug 派生板块固定 UUID（§7.1：UUIDv5(namespace, "community-board:{slug}")）。"""
    return uuid5(COMMUNITY_UUID_NAMESPACE, f"community-board:{slug}")


def source_deletion_id_for(user_id: UUID, source_ref: str) -> UUID:
    """稳定 source deletion event_id（§11.2 冻结幂等锚点）。"""
    return uuid5(
        COMMUNITY_UUID_NAMESPACE,
        f"community-source-deleted:{user_id}:{source_ref}",
    )
