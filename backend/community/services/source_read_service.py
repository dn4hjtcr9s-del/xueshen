"""CommunitySourceReadService：Memory 侧 ActivityReader 的服务端实现（方案 §10.4）。

职责（§10.4 冻结校验清单）：
1. activity_type 与 activity ID 前缀和数据库记录一致；
2. 记录的 user_id 等于请求目标 user_id（归属校验，不因 system principal 放行）；
3. 状态为 active（hidden/deleted 一律拒绝）；
4. content_ref 与稳定 source_ref 一致；
5. SourceBundle 字节数/单 item 长度/metadata 限制符合 Memory 契约；
6. 删除事实由 Memory 侧 DeletionAwareActivityReader 过滤（本服务只返回 active）。

SourceItem 约定（§10.4）：
- 帖子：source_ref=community:post:{id}，content="标题：{title}\\n正文：{body}"，
  仅返回作者自己的内容；
- 回复：source_ref=community:reply:{id}，content=回复正文，不拼入原帖正文；
- metadata 建议：source_version/board_slug/author_user_id/sequence（排障用，
  不发给总结模型）。
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.community.persistence import posts as posts_repo
from backend.community.persistence import replies as replies_repo
from backend.memory.contracts.errors import (
    SourceDeletedError,
    SourceNotFoundError,
    SourceTooLargeError,
)
from backend.memory.contracts.evidence import SourceBundle, SourceItem


class CommunitySourceReadService:
    """Source read 端口适配器（内部 HTTP 端点背后的实现，§10.4）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        logger: logging.Logger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._logger = logger or logging.getLogger("community.reader")

    async def read_source_bundle(
        self,
        *,
        user_id: UUID,
        activity_type: str,
        activity_ids: list[str],
        content_ref: str | None,
    ) -> SourceBundle:
        """读取并校验 SourceBundle（§10.4）。"""
        if len(activity_ids) != 1:
            # §10.4：Community 每次只返回一个 item；多 ID 拒绝
            raise SourceNotFoundError("来源不存在或无权访问")
        raw_id = activity_ids[0]
        try:
            if activity_type == "forum_post" and raw_id.startswith("post:"):
                item = await self._read_post(user_id, raw_id, content_ref)
            elif activity_type == "forum_reply" and raw_id.startswith("reply:"):
                item = await self._read_reply(user_id, raw_id, content_ref)
            else:
                # 1. activity_type 与 ID 前缀不匹配
                self._logger.warning(
                    "reader: activity_type/prefix mismatch: %s %s", activity_type, raw_id
                )
                raise SourceNotFoundError("来源不存在或无权访问")
            return SourceBundle.from_items([item])
        except SourceTooLargeError:
            raise
        except ValueError as exc:
            # pydantic ValidationError（ValueError 子类）：单 item 超限按契约转
            # SOURCE_TOO_LARGE（§10.4 校验 5）。SourceNotFoundError 非 ValueError，
            # 直接向上传播。
            raise SourceTooLargeError(str(exc)) from exc

    async def _read_post(self, user_id: UUID, raw_id: str, content_ref: str | None) -> SourceItem:
        try:
            post_id = UUID(raw_id.removeprefix("post:"))
        except ValueError as exc:
            raise SourceNotFoundError("来源不存在或无权访问") from exc
        source_ref = f"community:post:{post_id}"
        if content_ref is not None and content_ref != source_ref:
            # 4. content_ref 与稳定 source_ref 不一致
            self._logger.warning("reader: content_ref mismatch: %s", content_ref)
            raise SourceNotFoundError("来源不存在或无权访问")
        async with self._session_factory() as session:
            row = await posts_repo.get_post_any_status(session, post_id)
        if row is None:
            raise SourceNotFoundError("来源不存在或无权访问")
        # 2. 归属校验：只返回证据所属用户自己的内容
        if UUID(str(row["user_id"])) != user_id:
            self._logger.warning("reader: post ownership mismatch: %s", post_id)
            raise SourceNotFoundError("来源不存在或无权访问")
        status = str(row["status"])
        if status == "deleted":
            raise SourceDeletedError("来源已被删除")
        if status != "active" or not row["eligible_for_memory"]:
            # 3. 状态必须 active（hidden 对 Reader 同样拒绝，§9.4）
            self._logger.info("reader: post not readable: %s", post_id)
            raise SourceNotFoundError("来源不存在或无权访问")
        content = f"标题：{row['title']}\n正文：{row['body']}"
        return SourceItem(
            source_ref=source_ref,
            role="activity",
            content=content,
            occurred_at=row["created_at"],
            metadata={
                "source_version": row["content_hash"],
                "post_id": str(post_id),
                "board_slug": row["slug"],
                "author_user_id": str(user_id),
                "sequence": 1,
            },
        )

    async def _read_reply(self, user_id: UUID, raw_id: str, content_ref: str | None) -> SourceItem:
        try:
            reply_id = UUID(raw_id.removeprefix("reply:"))
        except ValueError as exc:
            raise SourceNotFoundError("来源不存在或无权访问") from exc
        source_ref = f"community:reply:{reply_id}"
        if content_ref is not None and content_ref != source_ref:
            self._logger.warning("reader: content_ref mismatch: %s", content_ref)
            raise SourceNotFoundError("来源不存在或无权访问")
        async with self._session_factory() as session:
            row = await replies_repo.get_reply_any_status(session, reply_id)
        if row is None:
            raise SourceNotFoundError("来源不存在或无权访问")
        if UUID(str(row["user_id"])) != user_id:
            self._logger.warning("reader: reply ownership mismatch: %s", reply_id)
            raise SourceNotFoundError("来源不存在或无权访问")
        status = str(row["status"])
        if status == "deleted":
            raise SourceDeletedError("来源已被删除")
        if status != "active" or not row["eligible_for_memory"]:
            self._logger.info("reader: reply not readable: %s", reply_id)
            raise SourceNotFoundError("来源不存在或无权访问")
        # §10.4：回复 content 只放回复正文，不拼入其他用户的原帖正文
        return SourceItem(
            source_ref=source_ref,
            role="activity",
            content=row["body"],
            occurred_at=row["created_at"],
            metadata={
                "source_version": row["content_hash"],
                "reply_id": str(reply_id),
                "post_id": str(row["post_id"]),
                "author_user_id": str(user_id),
                "sequence": 1,
            },
        )
