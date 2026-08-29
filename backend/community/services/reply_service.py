"""Community 回复写服务（方案 §8.4/§11.1，PR-C 纵切）。

- 创建回复：帖子 active 且 open 才可回复；deleted → POST_CLOSED（D31）、
  hidden → NOT_FOUND；事务内 reply_count+1、last_activity_at 更新（D30）、
  Outbox（forum_reply，topic_hints=帖子板块 slug）与 post_replied 通知；
- 删除回复：仅该回复 soft delete + source deletion Outbox（§11.1）；
  若为 solved_reply_id 同时清除（不递增 generation、不通知，D34）。

按 community-rebuild-plan.md v3.9 增补 board_slug 与条件删除。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.community import metrics
from backend.community.contracts.domain import source_deletion_id_for
from backend.community.contracts.errors import (
    CommunityIdempotencyConflictError,
    CommunityNotFoundError,
    CommunityPostClosedError,
)
from backend.community.persistence import idempotency as idem_repo
from backend.community.persistence import notifications as notifications_repo
from backend.community.persistence import outbox as outbox_repo
from backend.community.persistence import posts as posts_repo
from backend.community.persistence import replies as replies_repo
from backend.community.services import notification_templates
from backend.community.services.content_safety import reply_content_hash, validate_reply
from backend.community.services.public_user_profile_reader import PublicUserProfileReader
from backend.settings import Settings
from backend.shared.cursor import canonical_json

_DELETION_IDEMPOTENCY_KEY_PREFIX = "community:community.source_deleted:"


def _idempotency_payload_hash(values: dict[str, Any]) -> str:
    import hashlib

    return hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()


class ReplyService:
    """回复创建/删除写编排（§5.3/§5.4）。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        profile_reader_factory: Callable[[], PublicUserProfileReader],
        settings: Settings,
    ) -> None:
        self._session_factory = session_factory
        self._profile_reader_factory = profile_reader_factory
        self._settings = settings

    # ------------------------------------------------------------------
    # 回复（§8.4）
    # ------------------------------------------------------------------

    async def create_reply(
        self,
        *,
        user_id: UUID,
        post_id: UUID,
        body: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        profile = await self._profile_reader_factory().get_active_profile(user_id)
        body = validate_reply(body, max_chars=self._settings.community_reply_max_length)
        payload_hash = _idempotency_payload_hash({"post_id": str(post_id), "body": body})
        async with self._session_factory() as session:
            async with session.begin():
                reply_id = uuid4()
                inserted = await idem_repo.insert_request(
                    session,
                    user_id=user_id,
                    operation="create_reply",
                    idempotency_key=idempotency_key,
                    payload_hash=payload_hash,
                    resource_type="reply",
                    resource_id=reply_id,
                    retention_days=self._settings.community_idempotency_retention_days,
                )
                if not inserted:
                    existing = await idem_repo.get_request(
                        session,
                        user_id=user_id,
                        operation="create_reply",
                        idempotency_key=idempotency_key,
                    )
                    if existing is None:
                        raise CommunityIdempotencyConflictError("幂等键并发冲突且无法读取原资源")
                    if existing["payload_hash"] != payload_hash:
                        raise CommunityIdempotencyConflictError(
                            "同一 Idempotency-Key 提交了不同的请求体"
                        )
                    return await self._reply_view_for_user(UUID(str(existing["resource_id"])))
                post = await posts_repo.get_post_any_status(session, post_id)
                if post is None:
                    raise CommunityNotFoundError("帖子不存在或无权访问")
                if str(post["status"]) == "hidden":
                    raise CommunityNotFoundError("帖子不存在或无权访问")
                if str(post["status"]) == "deleted" or str(post["discussion_status"]) != "open":
                    raise CommunityPostClosedError("帖子已关闭，不能回复")
                await replies_repo.insert_reply(
                    session,
                    reply_id=reply_id,
                    post_id=post_id,
                    user_id=user_id,
                    author_display_name=profile.username,
                    body=body,
                    content_hash=reply_content_hash(body),
                )
                await posts_repo.bump_reply_activity(session, post_id)
                await self._enqueue_reply_created(
                    session, reply_id, post_id, user_id, post, reply_content_hash(body)
                )
                await self._notify_post_replied(
                    session,
                    post=post,
                    reply_id=reply_id,
                    actor=profile.username,
                    actor_user_id=user_id,
                    body=body,
                )
                metrics.community_reply_created_total.labels(board=post["slug"]).inc()
            return await self._reply_view_for_user(reply_id)

    # ------------------------------------------------------------------
    # 删除回复（§11.1 / §7.14）
    # ------------------------------------------------------------------

    async def delete_reply(self, *, actor_user_id: UUID, reply_id: UUID) -> None:
        """作者软删除回复；solved 回复同时清除解决标记（D34，不递增/不通知）。"""
        async with self._session_factory() as session:
            async with session.begin():
                reply = await replies_repo.get_reply_any_status(session, reply_id)
                if reply is None or str(reply["status"]) == "hidden":
                    raise CommunityNotFoundError("回复不存在或无权访问")
                if UUID(str(reply["user_id"])) != actor_user_id:
                    raise CommunityNotFoundError("回复不存在或无权访问")
                if str(reply["status"]) == "deleted":
                    return
                post_id = UUID(str(reply["post_id"]))
                deleted_rows = await replies_repo.mark_reply_deleted(session, reply_id)
                if deleted_rows == 0:
                    return
                await posts_repo.decrement_reply_count(session, post_id)
                post = await posts_repo.get_post_any_status(session, post_id)
                if post is not None and str(post.get("solved_reply_id")) == str(reply_id):
                    await posts_repo.set_solution(
                        session,
                        post_id,
                        reply_id=None,
                        generation=int(post["solution_generation"]),
                    )
                await self._enqueue_reply_source_deleted(
                    session,
                    user_id=UUID(str(reply["user_id"])),
                    reply_id=reply_id,
                )

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    async def _enqueue_reply_created(
        self,
        session: AsyncSession,
        reply_id: UUID,
        post_id: UUID,
        user_id: UUID,
        post: dict[str, Any],
        content_hash: str,
    ) -> None:
        """§7.5/§7.7 冻结 payload schema（community.reply_created）。"""
        event_type = "community.reply_created"
        await outbox_repo.insert_event(
            session,
            event_id=uuid4(),
            event_type=event_type,
            aggregate_type="reply",
            aggregate_id=str(reply_id),
            user_id=user_id,
            payload={
                "source_ref": f"community:reply:{reply_id}",
                "source_version": content_hash,
                "activity_type": "forum_reply",
                "activity_ids": [f"reply:{reply_id}"],
                "content_ref": f"community:reply:{reply_id}",
                "aggregated_count": 1,
                "topic_hints": [post["slug"]],
                "graph_node_hints": [],
                "window_started_at": None,
                "window_ended_at": None,
            },
            idempotency_key=f"community:{event_type}:{reply_id}",
        )

    async def _notify_post_replied(
        self,
        session: AsyncSession,
        *,
        post: dict[str, Any],
        reply_id: UUID,
        actor: str,
        actor_user_id: UUID,
        body: str,
    ) -> None:
        """§7.7/§7.8：帖子作者回复自己的帖子不产生 post_replied 通知。"""
        recipient = UUID(str(post["user_id"]))
        if recipient == actor_user_id:
            return
        dedupe = notifications_repo.dedupe_key(
            "post_replied", post_id=post["post_id"], reply_id=reply_id
        )
        await notifications_repo.insert_notification(
            session,
            notification_id=uuid4(),
            recipient_user_id=recipient,
            actor_user_id=actor_user_id,
            event_type="post_replied",
            post_id=post["post_id"],
            reply_id=reply_id,
            board_slug=post.get("slug"),
            title=notification_templates.post_replied_title(actor),
            body=notification_templates.post_replied_body(body),
            dedupe=dedupe,
        )

    async def _enqueue_reply_source_deleted(
        self, session: AsyncSession, *, user_id: UUID, reply_id: UUID
    ) -> None:
        """§11.2 冻结：稳定 event_id（UUIDv5）+ 幂等键。"""
        source_ref = f"community:reply:{reply_id}"
        event_id = source_deletion_id_for(user_id, source_ref)
        await outbox_repo.insert_event(
            session,
            event_id=event_id,
            event_type="community.source_deleted",
            aggregate_type="reply",
            aggregate_id=str(reply_id),
            user_id=user_id,
            payload={
                "source_ref": source_ref,
                "source_version": None,
                "source_system": "activity",
                "event_id": str(event_id),
            },
            idempotency_key=f"{_DELETION_IDEMPOTENCY_KEY_PREFIX}{source_ref.rsplit(':', 1)[-1]}",
        )

    async def _reply_view_for_user(self, reply_id: UUID) -> dict[str, Any]:
        """幂等重放：读取原回复行返回（未删除时）。"""
        async with self._session_factory() as session:
            reply = await replies_repo.get_reply_any_status(session, reply_id)
        if reply is None:
            raise CommunityNotFoundError("原回复不存在")
        return dict(reply)
