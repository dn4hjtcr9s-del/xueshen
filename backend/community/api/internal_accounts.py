"""Community 内部账号清理 API（方案 §8.8 / §11.4，PR-C 纵切）。

POST /api/v1/internal/community-accounts/purge：
- 仅允许 actor_type=system 且持 community:account_purge scope 的独立
  principal 调用（D36：system:community-purge）；
- 对该用户帖子/回复执行与作者删除相同的 deleted 语义（D16），
  为每个属于该用户的 activity source 写稳定 deletion Outbox（§11.2）；
- 幂等：重复调用/purge 重放不产生重复 deletion fact（outbox 键幂等）。

MVP 不新增面向用户的 Auth 删除账号入口（§11.4）；purge 是数据合规基础，
未来由统一 account deletion orchestrator 调用（发布阻塞项见 §11.4）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.auth.context import AuthContext
from backend.community.api.dependencies import get_community_runtime
from backend.community.contracts.domain import source_deletion_id_for
from backend.community.persistence import outbox as outbox_repo
from backend.community.persistence import posts as posts_repo
from backend.community.persistence import replies as replies_repo
from backend.shared.auth_context import require

#: D36：purge 仅限 system:community-purge principal
_SCOPE_ACCOUNT_PURGE = "community:account_purge"


class CommunityPurgeResult(BaseModel):
    """purge 响应（D35：镜像 MemoryOperationResult 同构）。"""

    model_config = ConfigDict(extra="forbid")

    operation_id: str
    status: str
    completed_at: str | None = None
    error: str | None = None


class CommunityPurgeRequest(BaseModel):
    """purge 请求体（§8.8：Auth 已先禁写，此处只需目标用户）。"""

    model_config = ConfigDict(extra="forbid")

    user_id: UUID


router = APIRouter(prefix="/api/v1/internal", tags=["internal-community"])


async def _enqueue_source_deleted(
    session: AsyncSession,
    *,
    user_id: UUID,
    source_ref: str,
    aggregate_type: str,
    aggregate_id: str,
) -> None:
    """§11.2 冻结：稳定 event_id（UUIDv5）+ 幂等键，重放不重复产生删除事实。"""
    event_id = source_deletion_id_for(user_id, source_ref)
    await outbox_repo.insert_event(
        session,
        event_id=event_id,
        event_type="community.source_deleted",
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        user_id=user_id,
        payload={
            "source_ref": source_ref,
            "source_version": None,
            "source_system": "activity",
            "event_id": str(event_id),
        },
        # D32：community:{event_type}:{aggregate_id}
        idempotency_key=f"community:community.source_deleted:{aggregate_id}",
    )


@router.post("/community-accounts/purge", response_model=CommunityPurgeResult)
async def purge_account(
    request: Request,
    payload: CommunityPurgeRequest,
    auth: AuthContext = Depends(require(actors=("system",), scope=_SCOPE_ACCOUNT_PURGE)),
) -> CommunityPurgeResult:
    """按用户执行普通删除语义（§8.8 步骤 1–3）。

    幂等：重复调用时 outbox 幂等键 ON CONFLICT DO NOTHING 天然去重，
    不产生重复 deletion fact（§11.2）。
    """
    runtime = get_community_runtime(request)
    session_factory = runtime.database.session_factory
    user_id = payload.user_id
    async with session_factory() as session:
        async with session.begin():
            # 1. 该用户帖子：deleted + closed + eligible=false（不级联他人回复）
            posts = (
                (
                    await session.execute(
                        text(
                            "SELECT post_id FROM community_posts "
                            "WHERE user_id = :user_id AND status <> 'deleted'"
                        ),
                        {"user_id": user_id},
                    )
                )
                .scalars()
                .all()
            )
            for post_id in posts:
                await posts_repo.mark_post_deleted(session, post_id)
                await _enqueue_source_deleted(
                    session,
                    user_id=user_id,
                    source_ref=f"community:post:{post_id}",
                    aggregate_type="post",
                    aggregate_id=str(post_id),
                )
            # 2. 该用户回复：deleted + eligible=false，维护 reply_count，
            #    必要时清除 solved_reply_id（purge 不使用 hidden，D16）
            replies = (
                (
                    await session.execute(
                        text(
                            "SELECT reply_id, post_id FROM community_replies "
                            "WHERE user_id = :user_id AND status <> 'deleted'"
                        ),
                        {"user_id": user_id},
                    )
                )
                .mappings()
                .all()
            )
            for row in replies:
                await replies_repo.mark_reply_deleted(session, row["reply_id"])
                await posts_repo.decrement_reply_count(session, row["post_id"])
                post = await posts_repo.get_post_any_status(session, row["post_id"])
                if post is not None and str(post.get("solved_reply_id")) == str(row["reply_id"]):
                    await posts_repo.set_solution(
                        session,
                        row["post_id"],
                        reply_id=None,
                        generation=int(post["solution_generation"]),
                    )
                await _enqueue_source_deleted(
                    session,
                    user_id=user_id,
                    source_ref=f"community:reply:{row['reply_id']}",
                    aggregate_type="reply",
                    aggregate_id=str(row["reply_id"]),
                )
    return CommunityPurgeResult(
        operation_id=f"community-purge:{user_id}",
        status="completed",
        completed_at=datetime.now(UTC).isoformat(),
    )
