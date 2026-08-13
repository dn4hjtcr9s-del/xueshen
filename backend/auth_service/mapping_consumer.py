"""身份映射补偿消费任务（方案 §3.2 / 附录 A.2 #7）。

- 只在 memory-api 进程内运行（app.py startup 启动 asyncio 后台任务）。
- 轮询 auth 库 identity_mapping_outbox（status=pending 且到期待补偿），向
  memory 库 account_identity_mappings 幂等插入（UNIQUE (issuer, external_subject)
  冲突即视为已存在）。
- 领取与状态写回在 auth 库同一事务内（FOR UPDATE SKIP LOCKED，可重入）；
  进程崩溃时事务回滚，事件自然回到可领取状态。
- 失败指数退避 30s 起步、封顶 1h；超过 20 次转 dead 并输出告警日志。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

#: 退避参数（方案 §3.2）：30s 起步，封顶 1h
_BASE_BACKOFF_SECONDS = 30.0
_MAX_BACKOFF_SECONDS = 3600.0

#: 最大尝试次数（方案 §3.2）：超限转 dead
_MAX_ATTEMPTS = 20


def backoff_seconds(attempts: int) -> float:
    return min(_MAX_BACKOFF_SECONDS, _BASE_BACKOFF_SECONDS * (2.0 ** max(attempts - 1, 0)))


def _utc_now() -> datetime:
    return datetime.now(UTC)


class IdentityMappingConsumer:
    """auth 库 outbox → memory 库 identity mappings 的可重入补偿循环。"""

    def __init__(
        self,
        *,
        auth_session_factory: async_sessionmaker[AsyncSession],
        memory_session_factory: async_sessionmaker[AsyncSession],
        poll_interval_seconds: float = 1.0,
        batch_size: int = 50,
        max_attempts: int = _MAX_ATTEMPTS,
        clock: Callable[[], datetime] = _utc_now,
        logger: logging.Logger | None = None,
    ) -> None:
        self._auth = auth_session_factory
        self._memory = memory_session_factory
        self._poll = poll_interval_seconds
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._clock = clock
        self._logger = logger or logging.getLogger("auth.mapping_consumer")
        self._stopping = asyncio.Event()

    def request_stop(self) -> None:
        self._stopping.set()

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:
                self._logger.exception("身份映射补偿 tick 出现未捕获异常")
            await asyncio.sleep(self._poll)

    async def tick(self) -> int:
        """领取一批待补偿事件并逐条处理；领取与写回同一 auth 事务。"""
        async with self._auth() as session:
            async with session.begin():
                result = await session.execute(
                    text(
                        """
                        SELECT event_id, user_id, issuer, external_subject, attempts
                        FROM identity_mapping_outbox
                        WHERE status = 'pending' AND next_attempt_at <= now()
                        ORDER BY next_attempt_at
                        LIMIT :batch
                        FOR UPDATE SKIP LOCKED
                        """
                    ),
                    {"batch": self._batch_size},
                )
                rows = [dict(r._mapping) for r in result.all()]
                if not rows:
                    return 0
                for row in rows:
                    await self._process(session, row)
        return len(rows)

    async def _process(self, auth_session: AsyncSession, row: dict[str, Any]) -> None:
        """幂等写入 memory 库映射；冲突必须核对归属（评审 P1-6），失败按退避重排。"""
        event_id = UUID(str(row["event_id"]))
        user_id = UUID(str(row["user_id"]))
        issuer = str(row["issuer"])
        external_subject = str(row["external_subject"])
        attempts = int(row["attempts"]) + 1

        mismatch = False
        failed = False
        try:
            async with self._memory() as session:
                async with session.begin():
                    await session.execute(
                        text(
                            """
                            INSERT INTO account_identity_mappings (
                                internal_user_id, issuer, external_subject
                            ) VALUES (:user_id, :issuer, :external_subject)
                            ON CONFLICT (issuer, external_subject) DO NOTHING
                            """
                        ),
                        {
                            "user_id": user_id,
                            "issuer": issuer,
                            "external_subject": external_subject,
                        },
                    )
                    # 评审 P1-6：冲突即视为已存在的前提是归属一致；
                    # 若 (issuer, external_subject) 已指向其他内部用户，
                    # 绝不能静默标记成功（会导致解析到错误用户）。
                    result = await session.execute(
                        text(
                            "SELECT internal_user_id FROM account_identity_mappings "
                            "WHERE issuer = :issuer AND external_subject = :external_subject"
                        ),
                        {"issuer": issuer, "external_subject": external_subject},
                    )
                    existing = result.scalar_one_or_none()
                    if existing is None or str(existing) != str(user_id):
                        mismatch = True
        except IntegrityError:
            # (internal_user_id, issuer) 主键冲突等：同样必须核对归属
            try:
                async with self._memory() as session:
                    result = await session.execute(
                        text(
                            "SELECT internal_user_id FROM account_identity_mappings "
                            "WHERE issuer = :issuer AND external_subject = :external_subject"
                        ),
                        {"issuer": issuer, "external_subject": external_subject},
                    )
                    existing = result.scalar_one_or_none()
                if existing is None or str(existing) != str(user_id):
                    mismatch = True
            except Exception as exc:
                failed = True
                self._logger.warning(
                    "身份映射冲突后核对失败 event=%s err=%s", event_id, type(exc).__name__
                )
        except Exception as exc:
            failed = True
            self._logger.warning(
                "身份映射补偿失败 event=%s user=%s err=%s",
                event_id,
                user_id,
                type(exc).__name__,
            )

        if mismatch:
            # 归属不一致无法通过重试自愈：直接转 dead 并告警，由运维介入
            await auth_session.execute(
                text(
                    "UPDATE identity_mapping_outbox SET status = 'dead', "
                    "attempts = :attempts, done_at = now() WHERE event_id = :event_id"
                ),
                {"attempts": attempts, "event_id": event_id},
            )
            self._logger.error(
                "告警：身份映射 (issuer=%s, sub=%s) 已指向其他内部用户，"
                "事件转 dead 需运维介入 event=%s",
                issuer,
                external_subject,
                event_id,
            )
        elif failed:
            if attempts >= self._max_attempts:
                await auth_session.execute(
                    text(
                        "UPDATE identity_mapping_outbox SET status = 'dead', "
                        "attempts = :attempts, done_at = now() WHERE event_id = :event_id"
                    ),
                    {"attempts": attempts, "event_id": event_id},
                )
                self._logger.error(
                    "告警：身份映射补偿超过 %d 次转 dead，需运维介入 event=%s user=%s",
                    self._max_attempts,
                    event_id,
                    user_id,
                )
            else:
                await auth_session.execute(
                    text(
                        "UPDATE identity_mapping_outbox SET attempts = :attempts, "
                        "next_attempt_at = :next WHERE event_id = :event_id"
                    ),
                    {
                        "attempts": attempts,
                        "next": self._clock() + timedelta(seconds=backoff_seconds(attempts)),
                        "event_id": event_id,
                    },
                )
        else:
            await auth_session.execute(
                text(
                    "UPDATE identity_mapping_outbox SET status = 'done', "
                    "attempts = :attempts, done_at = now() WHERE event_id = :event_id"
                ),
                {"attempts": attempts, "event_id": event_id},
            )
            self._logger.info("身份映射补偿完成 user=%s", user_id)
