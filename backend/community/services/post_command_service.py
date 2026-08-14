"""Community 帖子写服务（方案 §8.3–§8.5 / §11.1，PR-C 纵切）。

事务编排（§5.2–§5.4）：业务写入 + Outbox + 通知 + 幂等记录同一事务提交；
Memory 投递异步完成，不影响发布成功（D3）。

规则要点：
- user_id 全部来自认证上下文（§9.1）；创建前 profile 校验 active（§9.2）；
- 幂等表（§7.6/D40）：同键同 payload 返回原资源，不同 payload 冲突；
- 点赞/取消幂等（§7.4）；resolve 状态机（§8.5/D21/D34）；
- 删除软删除 + source deletion Outbox（§11.1/§11.2）；
- last_activity_at 仅发帖/回复创建更新（D30）。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.community import metrics
from backend.community.contracts.api import (
    CommunityPostDetail,
)
from backend.community.contracts.domain import (
    source_deletion_id_for,
)
from backend.community.contracts.errors import (
    CommunityBoardDisabledError,
    CommunityIdempotencyConflictError,
    CommunityNotFoundError,
    CommunityPostClosedError,
)
from backend.community.persistence import idempotency as idem_repo
from backend.community.persistence import likes as likes_repo
from backend.community.persistence import notifications as notifications_repo
from backend.community.persistence import outbox as outbox_repo
from backend.community.persistence import posts as posts_repo
from backend.community.persistence import replies as replies_repo
from backend.community.services import notification_templates
from backend.community.services.content_safety import (
    post_content_hash,
    validate_post,
)
from backend.community.services.public_user_profile_reader import (
    PublicUserProfileReader,
)
from backend.community.services.reply_service import ReplyService
from backend.shared.cursor import canonical_json

logger = logging.getLogger("community")

#: §11.2：deletion outbox 幂等键（D32 公式：community:{event_type}:{aggregate_id}）；
#: 与 Memory 侧删除幂等键（community-source-deleted:{user_id}:{source_ref}）是两层独立键。
_DELETION_IDEMPOTENCY_KEY_PREFIX = "community:community.source_deleted:"


def _idempotency_payload_hash(values: dict[str, Any]) -> str:
    """D40：canonical_json（确定性键排序）→ sha256。"""
    import hashlib

    return hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()


class PostCommandService:
    """帖子创建/点赞/解决/删除 + 回复创建/删除的写编排。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        profile_reader_factory: Callable[[], PublicUserProfileReader],
        reply_service: ReplyService,
    ) -> None:
        self._session_factory = session_factory
        self._profile_reader_factory = profile_reader_factory
        self._reply_service = reply_service

    # ------------------------------------------------------------------
    # 发帖（§8.3/§5.2）
    # ------------------------------------------------------------------

    async def create_post(
        self,
        *,
        user_id: UUID,
        board_id: UUID,
        title: str,
        body: str,
        idempotency_key: str,
        max_body_chars: int,
        idempotency_retention_days: int,
    ) -> CommunityPostDetail:
        profile = await self._profile_reader_factory().get_active_profile(user_id)
        title, body = validate_post(title, body, max_body_chars=max_body_chars)
        payload_hash = _idempotency_payload_hash(
            {"board_id": str(board_id), "title": title, "body": body}
        )
        async with self._session_factory() as session:
            async with session.begin():
                # Critical 1：先抢占幂等键（唯一约束为并发最终裁决），赢家才写业务；
                # 败者（并发同键）重读幂等行并返回原资源，绝不重复创建。
                post_id = uuid4()
                inserted = await idem_repo.insert_request(
                    session,
                    user_id=user_id,
                    operation="create_post",
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    resource_type="post",
                    resource_id=post_id,
                    retention_days=idempotency_retention_days,
                )
                if not inserted:
                    existing = await idem_repo.get_request(
                        session,
                        user_id=user_id,
                        operation="create_post",
                        idempotency_key=idempotency_key,
                    )
                    if existing is None:  # 并发冲突事务已提交，幂等行必然可见
                        raise CommunityIdempotencyConflictError("幂等键并发冲突且无法读取原资源")
                    return await self._replay_resource(
                        session, existing, payload_hash, operation="create_post"
                    )
                board = await self._active_board(session, board_id)
                content_hash = post_content_hash(title, body)
                await posts_repo.insert_post(
                    session,
                    post_id=post_id,
                    user_id=user_id,
                    author_display_name=profile.username,
                    board_id=board_id,
                    title=title,
                    body=body,
                    content_hash=content_hash,
                )
                await self._enqueue_post_created(session, post_id, user_id, board, content_hash)
                metrics.community_post_created_total.labels(board=board["slug"]).inc()
            return await self._detail_for_user(
                session_factory=self._session_factory, post_id=post_id, viewer_user_id=user_id
            )

    # ------------------------------------------------------------------
    # 点赞 / 取消点赞（§8.5/§7.4）
    # ------------------------------------------------------------------

    async def toggle_like(self, *, user_id: UUID, post_id: UUID, like: bool) -> None:
        """like=True 点赞、like=False 取消；对 deleted/hidden 帖统一 NOT_FOUND。"""
        async with self._session_factory() as session:
            async with session.begin():
                post = await self._require_visible_post(session, post_id)
                if str(post["status"]) == "deleted" or str(post["status"]) == "hidden":
                    raise CommunityNotFoundError("帖子不存在或无权访问")
                if like:
                    inserted = await likes_repo.insert_like(session, post_id, user_id)
                    if inserted:
                        await likes_repo.bump_like_count(session, post_id, 1)
                else:
                    removed = await likes_repo.delete_like(session, post_id, user_id)
                    if removed:
                        await likes_repo.bump_like_count(session, post_id, -1)

    # ------------------------------------------------------------------
    # 解决 / 取消解决（§8.5 状态机 / D21 / D34）
    # ------------------------------------------------------------------

    async def resolve(self, *, actor_user_id: UUID, post_id: UUID, reply_id: UUID | None) -> None:
        """标记解决/取消解决完整状态机（§8.5，v1.6 冻结）：

        - 从未解决 → 已解决，或 A→B 切换：solution_generation + 1，按新 generation 写通知；
        - 对当前同一 active reply 的幂等重试：不递增 generation、不重复通知；
        - 取消解决（reply_id=None）：只清空 solved_reply_id，不递增、不通知；
        - 删除 solved reply 时清除标记走 D34（不递增、不通知），不经过本方法；
        - closed/deleted（作者）→ POST_CLOSED；非作者/不可见 → NOT_FOUND。
        """
        async with self._session_factory() as session:
            async with session.begin():
                post = await posts_repo.get_post_any_status(session, post_id)
                if post is None or str(post["status"]) == "hidden":
                    raise CommunityNotFoundError("帖子不存在或无权访问")
                if UUID(str(post["user_id"])) != actor_user_id:
                    raise CommunityNotFoundError("帖子不存在或无权访问")
                if str(post["discussion_status"]) != "open" or str(post["status"]) == "deleted":
                    raise CommunityPostClosedError("帖子已关闭，不能修改解决状态")
                current_solved = post.get("solved_reply_id")
                if reply_id is None:
                    # 取消解决：幂等（未解决时同样返回成功）；不递增、不通知
                    if current_solved is not None:
                        await posts_repo.set_solution(
                            session,
                            post_id,
                            reply_id=None,
                            generation=int(post["solution_generation"]),
                        )
                    return
                reply = await replies_repo.get_reply_any_status(session, reply_id)
                if (
                    reply is None
                    or UUID(str(reply["post_id"])) != post_id
                    or str(reply["status"]) != "active"
                ):
                    raise CommunityNotFoundError("回复不存在或无权访问")
                # 幂等重试：对当前同一 active reply 不递增 generation、不重复通知
                if current_solved is not None and str(current_solved) == str(reply_id):
                    return
                new_generation = int(post["solution_generation"]) + 1
                await posts_repo.set_solution(
                    session, post_id, reply_id=reply_id, generation=new_generation
                )
                await self._notify_solved(
                    session,
                    post=post,
                    reply=reply,
                    generation=new_generation,
                    actor_user_id=actor_user_id,
                )

    async def _notify_solved(
        self,
        session: AsyncSession,
        *,
        post: dict[str, Any],
        reply: dict[str, Any],
        generation: int,
        actor_user_id: UUID,
    ) -> None:
        """§7.7：作者把自己的回复标记为解决时也不向自己发通知。"""
        recipient = UUID(str(reply["user_id"]))
        if recipient == actor_user_id:
            return

        dedupe = notifications_repo.dedupe_key(
            "reply_marked_solved",
            post_id=post["post_id"],
            reply_id=reply["reply_id"],
            solution_generation=generation,
        )
        await notifications_repo.insert_notification(
            session,
            notification_id=uuid4(),
            recipient_user_id=recipient,
            actor_user_id=actor_user_id,
            event_type="reply_marked_solved",
            post_id=post["post_id"],
            reply_id=reply["reply_id"],
            title=notification_templates.reply_marked_solved_title(),
            body=notification_templates.reply_marked_solved_body(str(post["title"])),
            dedupe=dedupe,
        )

    # ------------------------------------------------------------------
    # 删除帖子（§11.1）
    # ------------------------------------------------------------------

    async def delete_post(self, *, actor_user_id: UUID, post_id: UUID) -> None:
        """作者软删除帖子：closed + eligible=false + source deletion Outbox。

        重复删除已删除对象按幂等成功返回，不重复生成 deletion event（§8.5）。
        """
        async with self._session_factory() as session:
            async with session.begin():
                post = await posts_repo.get_post_any_status(session, post_id)
                if post is None or str(post["status"]) == "hidden":
                    raise CommunityNotFoundError("帖子不存在或无权访问")
                if UUID(str(post["user_id"])) != actor_user_id:
                    raise CommunityNotFoundError("帖子不存在或无权访问")
                if str(post["status"]) == "deleted":
                    return  # 幂等成功
                await posts_repo.mark_post_deleted(session, post_id)
                await self._enqueue_source_deleted(
                    session,
                    user_id=UUID(str(post["user_id"])),
                    source_ref=f"community:post:{post_id}",
                )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _replay_resource(
        self,
        session: AsyncSession,
        existing: dict[str, Any],
        payload_hash: str,
        *,
        operation: str,
    ) -> CommunityPostDetail:
        """幂等重放（§8.3）：同 hash 返回原资源；不同 hash → 冲突。"""
        if existing["payload_hash"] != payload_hash:
            raise CommunityIdempotencyConflictError("同一 Idempotency-Key 提交了不同的请求体")
        resource_id = UUID(str(existing["resource_id"]))
        post = await posts_repo.get_post_any_status(session, resource_id)
        if post is None:
            raise CommunityNotFoundError("原帖子不存在")
        # 幂等重放返回原资源（含墓碑契约；渲染路径与详情一致）
        from backend.community.services.post_service import PostReadService

        return PostReadService(self._session_factory)._detail_view(
            post, viewer_user_id=UUID(str(post["user_id"]))
        )

    async def _active_board(self, session: AsyncSession, board_id: UUID) -> dict[str, Any]:
        """板块校验（§8.7 冻结）：不存在 → 404 NOT_FOUND；存在但 hidden → 409
        BOARD_DISABLED（区分"无此板块"与"板块不可发帖"）。"""
        from backend.community.persistence import boards as boards_repo

        board = await boards_repo.get_board_any_status(session, board_id)
        if board is None:
            raise CommunityNotFoundError("板块不存在或无权访问")
        if str(board["status"]) != "active":
            raise CommunityBoardDisabledError("板块不可发帖")
        return board

    async def _require_visible_post(self, session: AsyncSession, post_id: UUID) -> dict[str, Any]:
        post = await posts_repo.get_post_any_status(session, post_id)
        if post is None:
            raise CommunityNotFoundError("帖子不存在或无权访问")
        return post

    async def _enqueue_post_created(
        self,
        session: AsyncSession,
        post_id: UUID,
        user_id: UUID,
        board: dict[str, Any],
        content_hash: str,
    ) -> None:
        """§7.5 冻结 payload schema（community.post_created）：source_version=content_hash。

        window_* 可空字段与 ActivityEvidence 对齐（§7.5），MVP 不设窗口。
        """
        event_type = "community.post_created"
        await outbox_repo.insert_event(
            session,
            event_id=uuid4(),
            event_type=event_type,
            aggregate_type="post",
            aggregate_id=str(post_id),
            user_id=user_id,
            payload={
                "source_ref": f"community:post:{post_id}",
                "source_version": content_hash,
                "activity_type": "forum_post",
                "activity_ids": [f"post:{post_id}"],
                "content_ref": f"community:post:{post_id}",
                "aggregated_count": 1,
                "topic_hints": [board["slug"]],
                "graph_node_hints": [],
                "window_started_at": None,
                "window_ended_at": None,
            },
            idempotency_key=f"community:{event_type}:{post_id}",
        )

    async def _enqueue_source_deleted(
        self, session: AsyncSession, *, user_id: UUID, source_ref: str
    ) -> None:
        """§11.2 冻结：稳定 event_id（UUIDv5）+ 幂等键，重放不重复产生删除事实。"""
        event_id = source_deletion_id_for(user_id, source_ref)
        await outbox_repo.insert_event(
            session,
            event_id=event_id,
            event_type="community.source_deleted",
            aggregate_type="post" if source_ref.startswith("community:post:") else "reply",
            aggregate_id=source_ref.rsplit(":", 1)[-1],
            user_id=user_id,
            payload={
                "source_ref": source_ref,
                "source_version": None,
                "source_system": "activity",
                "event_id": str(event_id),
            },
            idempotency_key=f"{_DELETION_IDEMPOTENCY_KEY_PREFIX}{source_ref.rsplit(':', 1)[-1]}",
        )

    async def _detail_for_user(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        post_id: UUID,
        viewer_user_id: UUID,
    ) -> CommunityPostDetail:
        from backend.community.services.post_service import PostReadService

        response, _ = await PostReadService(session_factory).get_post_detail(
            viewer_user_id=viewer_user_id,
            post_id=post_id,
            reply_after_key=None,
            reply_limit=20,
        )
        return response.post
