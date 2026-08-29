"""Community 通知标题/正文模板（§6.6 冻结，写入 community_notifications 时渲染）。

截断规则固定：正文/标题截断至 100 个 Unicode 字符，超出加 "…"（§6.6）。
已渲染文案不随作者改名追溯（已知限制，改名同步为 follow-up）。
"""

from __future__ import annotations

_NOTIFICATION_TRUNCATE_CHARS = 100
_ELLIPSIS = "…"


def _truncate(value: str, limit: int = _NOTIFICATION_TRUNCATE_CHARS) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + _ELLIPSIS


def post_replied_title(actor_display_name: str) -> str:
    """`{actor_display_name} 回复了你的帖子`（§6.6）。"""
    return f"{actor_display_name} 回复了你的帖子"


def post_replied_body(reply_body: str) -> str:
    """回复正文截断至 100 个 Unicode 字符，超出加 …（§6.6）。"""
    return _truncate(reply_body)


def reply_marked_solved_title() -> str:
    """`你的回复被标记为解决`（§6.6）。"""
    return "你的回复被标记为解决"


def reply_marked_solved_body(post_title: str) -> str:
    """帖子标题截断至 100 个 Unicode 字符，超出加 …（§6.6）。"""
    return _truncate(post_title)


def application_approved_body(name: str) -> str:
    """§7.8：你申请的板块「{name}」已通过审核。"""
    return f"你申请的板块「{name}」已通过审核"


def application_rejected_body(name: str, reason: str) -> str:
    """§7.8：你申请的板块「{name}」未通过审核：{reason}。"""
    return f"你申请的板块「{name}」未通过审核：{reason}"
