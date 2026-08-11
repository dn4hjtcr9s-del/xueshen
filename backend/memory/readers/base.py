"""Reader 与 SourceDeletionHandler 协议（§6.1 / §17.3 原文签名）。"""

from __future__ import annotations

from typing import Literal, Protocol
from uuid import UUID

from backend.memory.contracts.evidence import SourceBundle, SourceDeletedEvent


class ConversationReader(Protocol):
    """对话证据读取边界（§17.1）。

    正式适配器必须：自己校验消息属于该用户和线程；过滤 system/developer prompt、
    认证信息、隐藏推理和无关工具结果；助手消息只作上下文，不能单独证明用户掌握。
    """

    async def read(
        self,
        *,
        user_id: UUID,
        thread_id: str,
        checkpoint_id: str | None,
        message_ids: list[str],
    ) -> SourceBundle: ...


class ActivityReader(Protocol):
    """行为证据读取边界（§17.2）。

    page_view/bookmark/check_in 由上游聚合后提交；Memory 模块不采集网站埋点。
    """

    async def read(
        self,
        *,
        user_id: UUID,
        activity_type: str,
        activity_ids: list[str],
        content_ref: str | None,
    ) -> SourceBundle: ...


class SourceDeletionHandler(Protocol):
    """源删除事件处理（§17.3）。

    第一版只记录删除事实并阻止 Reader 再次返回该引用；
    "not_found" 保留给 v1.2 可查询上游的正式适配器，第一版不会产生。
    """

    async def handle(
        self,
        *,
        user_id: UUID,
        event: SourceDeletedEvent,
    ) -> Literal["recorded", "duplicate", "not_found"]: ...
