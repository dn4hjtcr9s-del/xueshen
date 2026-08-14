"""Community ActivityPublisher（方案 §12，v1.6 冻结）。

运行形态（§12.1/D8）：现有 FastAPI 进程的 lifespan background task，
不新增独立容器。每轮最多 claim COMMUNITY_OUTBOX_BATCH_SIZE 条：

1. 按事件类型和 feature flag claim（§12.1）：
   - COMMUNITY_MEMORY_SUBMIT_ENABLED=false → 不 claim post_created/reply_created
     （保持 pending，绝不标记 delivered/skipped，§10.1）；
   - COMMUNITY_SOURCE_DELETION_ENABLED 独立控制 source_deleted；
2. evidence 投递前重读 Community 状态（§11.3 删除竞态）：非 active →
   delivered + delivery_result=skipped_source_deleted；
3. 板块缺失/非 active → dead_letter + 稳定错误码 + 告警（§10.2/D22），
   不调用 Memory；
4. evidence 使用 issue_agent_token 签发短期 delegated activity_agent token
   （delegated_sub=事件 user_id，scope=memory:submit_evidence，§10.3）；
   deletion 使用独立 system token（COMMUNITY_SOURCE_DELETE_SERVICE_TOKEN，
   source_system=activity，§11.2）；
5. 写回沿用 Conversation fencing 语义（§7.5）：claim CAS 携带
   lease_generation，写回以 lease_owner + status='processing' 双条件。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.community import metrics
from backend.community.persistence import boards as boards_repo
from backend.community.persistence import outbox as outbox_repo
from backend.community.persistence import posts as posts_repo
from backend.community.persistence import replies as replies_repo
from backend.memory.client import MemoryClient
from backend.settings import Settings

logger = logging.getLogger("community.publisher")

#: 可重试错误码集合（§12.2）
_RETRYABLE_HTTP = {408, 409, 425, 429}
#: 最大退避（§12.2：参考 Conversation 的 1,800 秒）
_MAX_BACKOFF_SECONDS = 1800.0


class ActivityPublisherConfig:
    def __init__(self, settings: Settings) -> None:
        self.poll_seconds = settings.community_outbox_poll_seconds
        self.lease_seconds = settings.community_outbox_lease_seconds
        self.max_attempts = settings.community_outbox_max_attempts
        self.batch_size = settings.community_outbox_batch_size
        self.publisher_enabled = settings.community_publisher_enabled
        self.memory_submit_enabled = settings.community_memory_submit_enabled
        self.source_deletion_enabled = settings.community_source_deletion_enabled


class ActivityPublisher:
    """Community Outbox 轮询投递器（lifespan background task，§12.1）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: ActivityPublisherConfig,
        source_delete_client: MemoryClient | None,
        agent_token_factory: Any,
        worker_id: str,
        memory_client: MemoryClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._memory_client = memory_client
        self._source_delete_client = source_delete_client
        # issue_agent_token 同进程契约（§10.3/D8）：受信进程内签发短期 token。
        # token_provider 按当前事件 user_id 签发（串行轮询下无并发风险）。
        self._agent_token_factory = agent_token_factory
        self._current_delegated_sub: str | None = None
        self.worker_id = worker_id
        self._stop = asyncio.Event()
        self._rng = random.Random()

    def set_memory_client(self, memory_client: MemoryClient) -> None:
        """注入 evidence 客户端（装配顺序：token_provider 依赖本实例）。"""
        self._memory_client = memory_client

    def _agent_token(self) -> str:
        """为当前事件签发短期 delegated activity_agent token（§10.3）。"""
        if self._current_delegated_sub is None:
            raise RuntimeError("Publisher 未设置当前委托用户")
        token = self._agent_token_factory(
            f"community-publisher-{self.worker_id}",
            self._current_delegated_sub,
            ["memory:submit_evidence"],
        )
        return str(token)

    async def run_forever(self) -> None:
        if not self._config.publisher_enabled:
            logger.info("ActivityPublisher 未启用（COMMUNITY_PUBLISHER_ENABLED=false），退出")
            return
        logger.info("ActivityPublisher 启动: %s", self.worker_id)
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception:
                logger.exception("publisher 轮询周期异常")
            await asyncio.sleep(self._config.poll_seconds)

    async def stop(self) -> None:
        self._stop.set()

    async def _claimable_event_types(self) -> tuple[str, ...]:
        """§12.1 步骤 1：按 feature flag 决定可 claim 的事件类型。"""
        types: list[str] = []
        if self._config.memory_submit_enabled:
            types.extend(["community.post_created", "community.reply_created"])
        if self._config.source_deletion_enabled:
            types.append("community.source_deleted")
        return tuple(types)

    async def _poll_once(self) -> None:
        event_types = await self._claimable_event_types()
        if not event_types:
            return
        async with self._session_factory() as session:
            async with session.begin():
                rows = await outbox_repo.claim_events(
                    session,
                    worker_id=self.worker_id,
                    lease_seconds=self._config.lease_seconds,
                    batch_size=self._config.batch_size,
                    allowed_event_types=event_types,
                )
        await self._update_outbox_gauges()
        for row in rows:
            started = time.monotonic()
            status = await self._deliver(row)
            metrics.community_activity_publish_latency_seconds.observe(time.monotonic() - started)
            metrics.community_activity_publish_total.labels(
                activity_type=str(row["payload"].get("activity_type", "source_deleted")),
                status=status,
            ).inc()

    async def _update_outbox_gauges(self) -> None:
        """§12.3：每轮更新 pending 计数与最老事件年龄 gauge。"""
        async with self._session_factory() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT event_type, COUNT(*) AS cnt, MIN(created_at) AS oldest "
                            "FROM community_outbox "
                            "WHERE status IN ('pending', 'retry_wait') "
                            "GROUP BY event_type"
                        )
                    )
                )
                .mappings()
                .all()
            )
        now = datetime.now(UTC)
        seen: set[str] = set()
        for row in rows:
            event_type = str(row["event_type"])
            seen.add(event_type)
            metrics.community_outbox_pending_total.labels(event_type=event_type).set(
                int(row["cnt"])
            )
            oldest = row["oldest"]
            if oldest is not None:
                metrics.community_outbox_oldest_age_seconds.labels(event_type=event_type).set(
                    max(0.0, (now - oldest).total_seconds())
                )
        # 未出现的类型清零（避免陈旧计数）
        for event_type in (
            "community.post_created",
            "community.reply_created",
            "community.source_deleted",
        ):
            if event_type not in seen:
                metrics.community_outbox_pending_total.labels(event_type=event_type).set(0)
                metrics.community_outbox_oldest_age_seconds.labels(event_type=event_type).set(0)

    async def _deliver(self, row: dict[str, Any]) -> str:
        """投递单条 Outbox；返回最终状态（published/skipped/dead_letter/retry_wait）。"""
        event_type = str(row["event_type"])
        if event_type == "community.source_deleted":
            return await self._deliver_source_deletion(row)
        return await self._deliver_evidence(row)

    # ------------------------------------------------------------------
    # evidence（§12.1 步骤 2–5）
    # ------------------------------------------------------------------

    async def _deliver_evidence(self, row: dict[str, Any]) -> str:
        payload = row["payload"]
        # 步骤 3：删除竞态——投递前重读 Community 状态（§11.3）
        item = await self._read_activity_source(payload)
        if item is None:
            # 记录不存在或非 active：delivered + skipped_source_deleted（§11.3）
            async with self._session_factory() as session:
                async with session.begin():
                    await outbox_repo.mark_delivered(
                        session,
                        row["event_id"],
                        worker_id=self.worker_id,
                        delivery_result="skipped_source_deleted",
                    )
            logger.info("evidence skipped（来源已删除/非 active）: event=%s", row["event_id"])
            return "skipped_source_deleted"
        # 步骤 4：板块校验（§10.2/D22）：缺失/非 active → dead_letter，不调用 Memory
        board = await self._read_board(payload, item)
        if board is None or str(board["status"]) != "active":
            code = "community_board_missing" if board is None else "community_board_inactive"
            logger.error("evidence 板块异常进入 dead-letter: %s event=%s", code, row["event_id"])
            async with self._session_factory() as session:
                async with session.begin():
                    await outbox_repo.mark_dead_letter(
                        session, row["event_id"], worker_id=self.worker_id, error_code=code
                    )
            return "dead_letter"
        # 步骤 5：短期 delegated activity_agent token + 提交（§10.3）
        activity_type = str(payload["activity_type"])
        activity_ids = list(payload["activity_ids"])
        idempotency_key = f"community-activity:{activity_type}:{activity_ids[0]}:v1"
        self._current_delegated_sub = str(row["user_id"])
        if self._memory_client is None:
            raise RuntimeError("MemoryClient 尚未注入（Publisher 装配不完整）")
        try:
            await self._memory_client.submit_activity_evidence(
                idempotency_key=idempotency_key,
                activity_type=activity_type,  # type: ignore[arg-type]
                activity_ids=activity_ids,
                content_ref=payload.get("content_ref"),
                aggregated_count=int(payload.get("aggregated_count", 1)),
                topic_hints=list(payload.get("topic_hints", [])),
                graph_node_hints=list(payload.get("graph_node_hints", [])),
            )
        except Exception as exc:
            return await self._handle_failure(row, exc)
        # 步骤 7：写回 delivered（fencing）
        async with self._session_factory() as session:
            async with session.begin():
                await outbox_repo.mark_delivered(
                    session,
                    row["event_id"],
                    worker_id=self.worker_id,
                    delivery_result="published",
                )
        return "published"

    # ------------------------------------------------------------------
    # source deletion（§11.2）
    # ------------------------------------------------------------------

    async def _deliver_source_deletion(self, row: dict[str, Any]) -> str:
        if self._source_delete_client is None:
            await self._dead_letter(row, "SOURCE_DELETION_CLIENT_MISSING")
            return "dead_letter"
        payload = row["payload"]
        try:
            await self._source_delete_client.submit_source_deletion(
                idempotency_key=f"community-source-deleted:{row['user_id']}:{payload['source_ref']}",
                user_id=row["user_id"],
                source_ref=payload["source_ref"],
                source_version=None,
                event_id=UUID(str(payload["event_id"])),
                source_system="activity",
            )
        except Exception as exc:
            return await self._handle_failure(row, exc)
        async with self._session_factory() as session:
            async with session.begin():
                await outbox_repo.mark_delivered(
                    session, row["event_id"], worker_id=self.worker_id,
                    delivery_result="published",
                )
        # §12.3：source deletion 投递延迟（事件创建到投递成功）
        metrics.community_source_deletion_lag_seconds.observe(
            max(0.0, (datetime.now(UTC) - row["created_at"]).total_seconds())
        )
        return "published"

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    async def _read_activity_source(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """重读 Community 记录（§11.3）：非 active/不存在返回 None。"""
        source_ref = str(payload["source_ref"])
        async with self._session_factory() as session:
            if source_ref.startswith("community:post:"):
                post_id = UUID(source_ref.rsplit(":", 1)[-1])
                row = await posts_repo.get_post_any_status(session, post_id)
                if row is None or str(row["status"]) != "active" or not row["eligible_for_memory"]:
                    return None
                return row
            if source_ref.startswith("community:reply:"):
                reply_id = UUID(source_ref.rsplit(":", 1)[-1])
                row = await replies_repo.get_reply_any_status(session, reply_id)
                if row is None or str(row["status"]) != "active" or not row["eligible_for_memory"]:
                    return None
                return row
        return None

    async def _read_board(
        self, payload: dict[str, Any], item: dict[str, Any]
    ) -> dict[str, Any] | None:
        """按来源类型读取所属板块（§10.2/D22 校验用）。"""
        async with self._session_factory() as session:
            if item.get("board_id") is not None:
                return await boards_repo.get_board_any_status(session, item["board_id"])
            # 回复无 board_id 列：由所属帖子查询
            if payload["source_ref"].startswith("community:reply:"):
                post_id = UUID(str(item["post_id"]))
                post = await posts_repo.get_post_any_status(session, post_id)
                if post is None:
                    return None
                return await boards_repo.get_board_any_status(session, post["board_id"])
        return None

    async def _handle_failure(self, row: dict[str, Any], exc: Exception) -> str:
        """错误分类（§12.2）：永久错误直接 dead_letter；可重试按指数退避。"""
        from backend.memory.client import MemoryClientError

        code = type(exc).__name__
        permanent = False
        if isinstance(exc, MemoryClientError):
            status = exc.http_status
            permanent = status < 500 and status not in _RETRYABLE_HTTP
            code = exc.code
        attempt = int(row["attempt_count"])
        async with self._session_factory() as session:
            async with session.begin():
                if permanent or attempt >= self._config.max_attempts:
                    await outbox_repo.mark_dead_letter(
                        session, row["event_id"], worker_id=self.worker_id, error_code=code
                    )
                    logger.error(
                        "evidence 投递永久失败进入 dead-letter: %s event=%s",
                        code,
                        row["event_id"],
                    )
                    return "dead_letter"
                next_attempt = time.time() + self._backoff(attempt)
                await outbox_repo.mark_retry_wait(
                    session,
                    row["event_id"],
                    worker_id=self.worker_id,
                    error_code=code,
                    next_attempt_at=_epoch_to_dt(next_attempt),
                )
                return "retry_wait"

    def _backoff(self, attempt: int) -> float:
        """指数退避 + 抖动（§12.2：cap 1800s）。"""
        base = min(2.0**attempt, _MAX_BACKOFF_SECONDS)
        return min(base * (0.5 + self._rng.random()), _MAX_BACKOFF_SECONDS)

    async def _dead_letter(self, row: dict[str, Any], code: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await outbox_repo.mark_dead_letter(
                    session, row["event_id"], worker_id=self.worker_id, error_code=code
                )


def _epoch_to_dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)
