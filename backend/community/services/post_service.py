"""Community 只读服务（方案 §8.1–§8.4，PR-B 纵切）。

- 板块列表：仅 active（§8.1）；
- 帖子列表：active + latest/unanswered + keyset 游标分页（§8.2）；
- 帖子详情：hidden → NOT_FOUND（含作者）；deleted → 墓碑契约（§6.6）；
- 回复分页：created_at ASC keyset，游标绑定具体 post_id（D39）。

按 community-rebuild-plan.md v3.9 增补：attachments 恒返回、匿名读、board detail。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.community.contracts.api import (
    BoardDetailResponse,
    CommunityAttachment,
    CommunityAuthor,
    CommunityBoard,
    CommunityPostDetail,
    CommunityPostDetailResponse,
    CommunityPostSummary,
    CommunityReplyView,
    Page,
)
from backend.community.contracts.errors import CommunityNotFoundError
from backend.community.persistence import attachments as attachments_repo
from backend.community.persistence import boards as boards_repo
from backend.community.persistence import posts as posts_repo
from backend.community.persistence import replies as replies_repo
from backend.community.storage.base import StorageBackend
from backend.settings import Settings

logger = logging.getLogger("community")


class PostReadService:
    """帖子/板块/回复只读查询（viewer 视角渲染公共 DTO）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: Settings,
        storage: StorageBackend | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings
        self._storage = storage

    # ------------------------------------------------------------------
    # 板块（§8.1 / §八 #1-#2）
    # ------------------------------------------------------------------

    async def list_boards(self) -> list[BoardDetailResponse]:
        async with self._session_factory() as session:
            rows = await boards_repo.list_active_boards(session)
        return [self._board_detail_view(row, viewer_user_id=None) for row in rows]

    async def get_board_detail_by_slug(
        self, slug: str, viewer_user_id: UUID | None
    ) -> BoardDetailResponse:
        async with self._session_factory() as session:
            row = await boards_repo.get_active_board_by_slug(session, slug)
        if row is None:
            raise CommunityNotFoundError("板块不存在或无权访问")
        return self._board_detail_view(row, viewer_user_id)

    # ------------------------------------------------------------------
    # 帖子列表（§8.2）
    # ------------------------------------------------------------------

    async def list_posts(
        self,
        *,
        viewer_user_id: UUID | None,
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
            liked: set[UUID] = set()
            if viewer_user_id is not None and page_rows:
                liked = await posts_repo.liked_post_ids(
                    session, [r["post_id"] for r in page_rows], viewer_user_id
                )
            attachments_map = await self._attachments_for_posts(session, page_rows)
        items = [
            self._to_summary(row, viewer_user_id, liked, attachments_map.get(row["post_id"], []))
            for row in page_rows
        ]
        next_key: tuple[Any, ...] | None = None
        if page_rows:
            last = page_rows[-1]
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
        viewer_user_id: UUID | None,
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
                raise CommunityNotFoundError("帖子不存在或无权访问")
            replies = await replies_repo.list_replies_page(
                session,
                post_id=post_id,
                after=reply_after_key,
                limit=reply_limit,
            )
            liked: set[UUID] = set()
            if viewer_user_id is not None:
                liked = await posts_repo.liked_post_ids(session, [post_id], viewer_user_id)
            attachments = await self._attachments_for_post(session, post_id)
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
            post=self._to_detail(post, viewer_user_id, liked, attachments),
            replies=Page[CommunityReplyView](
                items=replies_view,
                next_cursor=None,
                has_more=reply_has_more,
            ),
        )
        return response, reply_next

    # ------------------------------------------------------------------
    # DTO 渲染
    # ------------------------------------------------------------------

    def _detail_view(
        self,
        row: dict[str, Any],
        *,
        viewer_user_id: UUID | None,
        liked: set[UUID] | None = None,
        attachments: list[CommunityAttachment] | None = None,
    ) -> CommunityPostDetail:
        """单行帖子 → 详情 DTO（供幂等重放等无需 DB 渲染的路径复用）。"""
        return self._to_detail(
            row,
            viewer_user_id,
            liked=liked or set(),
            attachments=attachments or [],
        )

    @staticmethod
    def _board_view(row: dict[str, Any]) -> CommunityBoard:
        return CommunityBoard(
            board_id=row["board_id"],
            slug=row["slug"],
            name=row["name"],
            description=row["description"],
        )

    def _board_detail_view(
        self, row: dict[str, Any], viewer_user_id: UUID | None
    ) -> BoardDetailResponse:
        created_by = row.get("created_by")
        return BoardDetailResponse(
            board_id=row["board_id"],
            slug=row["slug"],
            name=row["name"],
            description=row["description"],
            post_count=int(row["post_count"]),
            created_at=row["created_at"],
            viewer_is_owner=(
                viewer_user_id is not None
                and created_by is not None
                and UUID(str(created_by)) == viewer_user_id
            ),
        )

    @staticmethod
    def _author_view(row: dict[str, Any]) -> CommunityAuthor:
        return CommunityAuthor(display_name=str(row["author_display_name"]))

    def _to_summary(
        self,
        row: dict[str, Any],
        viewer_user_id: UUID | None,
        liked: set[UUID],
        attachments: list[CommunityAttachment],
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
            viewer_liked=(viewer_user_id is not None and row["post_id"] in liked),
            attachments=attachments,
            created_at=row["created_at"],
            last_activity_at=row["last_activity_at"],
        )

    def _to_detail(
        self,
        row: dict[str, Any],
        viewer_user_id: UUID | None,
        liked: set[UUID],
        attachments: list[CommunityAttachment],
    ) -> CommunityPostDetail:
        deleted = str(row["status"]) == "deleted"
        # 已删除帖子遵循墓碑契约：不泄露标题/正文/附件 URL
        if deleted:
            attachments = []
        solved_reply_id = row.get("solved_reply_id")
        data = self._to_summary(row, viewer_user_id, liked, attachments).model_dump()
        if deleted:
            data["title"] = None
        is_author = viewer_user_id is not None and UUID(str(row["user_id"])) == viewer_user_id
        return CommunityPostDetail(
            **data,
            body=None if deleted else row["body"],
            deleted=deleted,
            discussion_status=str(row["discussion_status"]),  # type: ignore[arg-type]
            viewer_is_author=is_author,
            solved_reply_id=UUID(str(solved_reply_id)) if solved_reply_id else None,
            deleted_at=row.get("deleted_at"),
        )

    @staticmethod
    def _to_reply(
        row: dict[str, Any], viewer_user_id: UUID | None, *, solved: bool
    ) -> CommunityReplyView:
        deleted = str(row["status"]) == "deleted"
        is_author = viewer_user_id is not None and UUID(str(row["user_id"])) == viewer_user_id
        return CommunityReplyView(
            reply_id=row["reply_id"],
            author=CommunityAuthor(display_name=str(row["author_display_name"])),
            body=None if deleted else row["body"],
            deleted=deleted,
            viewer_is_author=is_author,
            solved=solved,
            created_at=row["created_at"],
        )

    # ------------------------------------------------------------------
    # 附件渲染
    # ------------------------------------------------------------------

    async def _attachments_for_posts(
        self, session: AsyncSession, rows: list[dict[str, Any]]
    ) -> dict[UUID, list[CommunityAttachment]]:
        if not rows:
            return {}
        post_ids = [r["post_id"] for r in rows]
        attachment_rows = await attachments_repo.get_attachments_for_posts(session, post_ids)
        result: dict[UUID, list[CommunityAttachment]] = {pid: [] for pid in post_ids}
        url_provider = self._storage or _NoopUrlProvider()
        for row in attachment_rows:
            result.setdefault(row["post_id"], []).append(self._attachment_view(row, url_provider))
        return result

    async def _attachments_for_post(
        self, session: AsyncSession, post_id: UUID
    ) -> list[CommunityAttachment]:
        rows = await attachments_repo.get_attachments_by_post_id(session, post_id)
        url_provider = self._storage or _NoopUrlProvider()
        return [self._attachment_view(row, url_provider) for row in rows]

    @staticmethod
    def _attachment_view(row: dict[str, Any], url_provider: Any) -> CommunityAttachment:
        return CommunityAttachment(
            attachment_id=row["attachment_id"],
            url=url_provider.public_url(row["storage_key"]),
            width=int(row["width"]),
            height=int(row["height"]),
            mime=str(row["mime"]),
            position=int(row["position"]),
        )


class _NoopUrlProvider:
    """未装配 storage 时的兜底（仅用于 service 独立渲染路径）。"""

    def public_url(self, key: str) -> str:
        return key
