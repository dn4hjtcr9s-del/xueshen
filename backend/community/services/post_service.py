"""Community 只读服务（方案 §8.1–§8.4，PR-B 纵切）。

- 板块列表：仅 active（§8.1）；
- 帖子列表：active + latest/unanswered + keyset 游标分页（§8.2）；
- 帖子详情：hidden → NOT_FOUND（含作者）；deleted → 墓碑契约（§6.6）；
- 回复分页：created_at ASC keyset，游标绑定具体 post_id（D39）。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.community.contracts.api import (
    CommunityAuthor,
    CommunityBoard,
    CommunityPostDetail,
    CommunityPostDetailResponse,
    CommunityPostSummary,
    CommunityReplyView,
    Page,
)
from backend.community.contracts.errors import CommunityNotFoundError
from backend.community.persistence import boards as boards_repo
from backend.community.persistence import posts as posts_repo
from backend.community.persistence import replies as replies_repo

logger = logging.getLogger("community")


class PostReadService:
    """帖子/板块/回复只读查询（viewer 视角渲染公共 DTO）。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # 板块（§8.1）
    # ------------------------------------------------------------------

    async def list_boards(self) -> list[CommunityBoard]:
        async with self._session_factory() as session:
            rows = await boards_repo.list_active_boards(session)
        return [
            CommunityBoard(
                board_id=row["board_id"],
                slug=row["slug"],
                name=row["name"],
                description=row["description"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # 帖子列表（§8.2）
    # ------------------------------------------------------------------

    async def list_posts(
        self,
        *,
        viewer_user_id: UUID,
        board_id: UUID | None,
        sort: str,
        after_key: tuple[Any, ...] | None,
        limit: int,
    ) -> tuple[list[CommunityPostSummary], tuple[Any, ...] | None, bool]:
        """返回 (items, next_after_key, has_more)。after_key 与游标 sort_key 一一对应。"""
        async with self._session_factory() as session:
            rows = await posts_repo.list_posts(
                session,
                board_id=board_id,
                sort=sort,
                after=after_key,
                limit=limit,
            )
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            liked = await posts_repo.liked_post_ids(
                session, [r["post_id"] for r in page_rows], viewer_user_id
            )
        items = [self._to_summary(row, viewer_user_id, liked) for row in page_rows]
        next_key: tuple[Any, ...] | None = None
        if page_rows:
            last = page_rows[-1]
            # 排序键需 JCS 可序列化（canonical_json 不支持 datetime）：转 ISO 字符串
            next_key = (
                bool(last["pinned"]),
                last["last_activity_at"].isoformat(),
                str(last["post_id"]),
            )
        return items, next_key, has_more

    # ------------------------------------------------------------------
    # 帖子详情（§8.4 / §6.6 墓碑契约）
    # ------------------------------------------------------------------

    async def get_post_detail(
        self,
        *,
        viewer_user_id: UUID,
        post_id: UUID,
        reply_after_key: tuple[Any, ...] | None,
        reply_limit: int,
    ) -> tuple[CommunityPostDetailResponse, tuple[Any, ...] | None]:
        """返回 (详情响应, 回复下一页 keyset key)；游标由路由层签发。"""
        async with self._session_factory() as session:
            post = await posts_repo.get_post_any_status(session, post_id)
            if post is None:
                raise CommunityNotFoundError("帖子不存在或无权访问")
            status = str(post["status"])
            if status == "hidden":
                # §8.4：hidden 对所有人（含作者）表现为 NOT_FOUND，不泄露状态
                raise CommunityNotFoundError("帖子不存在或无权访问")
            replies = await replies_repo.list_replies_page(
                session,
                post_id=post_id,
                after=reply_after_key,
                limit=reply_limit,
            )
            liked = await posts_repo.liked_post_ids(session, [post_id], viewer_user_id)
        reply_has_more = len(replies) > reply_limit
        reply_page = replies[:reply_limit]
        reply_next: tuple[Any, ...] | None = None
        if reply_page:
            last = reply_page[-1]
            reply_next = (last["created_at"].isoformat(), str(last["reply_id"]))
        solved_reply_id = post.get("solved_reply_id")
        solved = UUID(str(solved_reply_id)) if solved_reply_id else None
        replies_view = [
            self._to_reply(row, viewer_user_id, solved=solved == row["reply_id"])
            for row in reply_page
        ]
        response = CommunityPostDetailResponse(
            post=self._to_detail(post, viewer_user_id, liked),
            replies=Page[CommunityReplyView](
                items=replies_view,
                next_cursor=None,  # 由路由层签发游标（service 不接触签名）
                has_more=reply_has_more,
            ),
        )
        return response, reply_next

    # ------------------------------------------------------------------
    # DTO 渲染
    # ------------------------------------------------------------------

    def _detail_view(self, row: dict[str, Any], *, viewer_user_id: UUID) -> CommunityPostDetail:
        """单行帖子 → 详情 DTO（供幂等重放等无需 DB 渲染的路径复用）。"""
        return self._to_detail(row, viewer_user_id, liked=set())

    @staticmethod
    def _board_view(row: dict[str, Any]) -> CommunityBoard:
        return CommunityBoard(
            board_id=row["board_id"],
            slug=row["slug"],
            name=row["name"],
            description=row["description"],
        )

    @staticmethod
    def _author_view(row: dict[str, Any]) -> CommunityAuthor:
        return CommunityAuthor(display_name=str(row["author_display_name"]))

    def _to_summary(
        self,
        row: dict[str, Any],
        viewer_user_id: UUID,
        liked: set[UUID],
    ) -> CommunityPostSummary:
        return CommunityPostSummary(
            post_id=row["post_id"],
            board=self._board_view(row),
            author=self._author_view(row),
            title=str(row["title"]),
            pinned=bool(row["pinned"]),
            solved=row["solved_reply_id"] is not None,
            reply_count=int(row["reply_count"]),
            like_count=int(row["like_count"]),
            viewer_liked=row["post_id"] in liked,
            created_at=row["created_at"],
            last_activity_at=row["last_activity_at"],
        )

    def _to_detail(
        self,
        row: dict[str, Any],
        viewer_user_id: UUID,
        liked: set[UUID],
    ) -> CommunityPostDetail:
        deleted = str(row["status"]) == "deleted"
        solved_reply_id = row.get("solved_reply_id")
        data = self._to_summary(row, viewer_user_id, liked).model_dump()
        if deleted:
            # §6.6 墓碑契约：deleted 帖子 title/body 均为 null，不泄露原正文
            data["title"] = None
        return CommunityPostDetail(
            **data,
            body=None if deleted else row["body"],
            deleted=deleted,
            discussion_status=str(row["discussion_status"]),  # type: ignore[arg-type]
            viewer_is_author=UUID(str(row["user_id"])) == viewer_user_id,
            solved_reply_id=UUID(str(solved_reply_id)) if solved_reply_id else None,
            deleted_at=row.get("deleted_at"),
        )

    @staticmethod
    def _to_reply(row: dict[str, Any], viewer_user_id: UUID, *, solved: bool) -> CommunityReplyView:
        deleted = str(row["status"]) == "deleted"
        return CommunityReplyView(
            reply_id=row["reply_id"],
            author=CommunityAuthor(display_name=str(row["author_display_name"])),
            body=None if deleted else row["body"],
            deleted=deleted,
            viewer_is_author=UUID(str(row["user_id"])) == viewer_user_id,
            solved=solved,
            created_at=row["created_at"],
        )
