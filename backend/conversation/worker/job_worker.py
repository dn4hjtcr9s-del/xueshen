"""conversation-job worker：可靠消费 conversation_jobs（方案 §7.7 / §8.6）。

- generate_title：首个 Turn 完成后触发；模型失败取首条用户消息前 20 字符兜底（§7.6）；
- summarize_thread：未覆盖消息累计达 8000 tokens（tiktoken o200k_base，附录 A.7）
  时触发；失败不阻塞主对话；
- delete_thread：deleting → deleted 的唯一协调器（§1.5 R3 / §8.6）。

全部采用 lease/fencing 原则；Job 状态机 pending → processing → done / retry_wait / dead_letter。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.persistence import jobs as jobs_repo
from backend.conversation.services.thread_deletion import execute_delete_thread
from backend.conversation.services.token_counter import TokenCounter

logger = logging.getLogger("conversation.worker.jobs")

TITLE_FALLBACK_CHARS = 20
SUMMARY_TRIGGER_TOKENS = 8000


class JobWorker:
    """标题/摘要/删除 Job 消费器（§7.6 / §7.7 / §8.6）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        config: Any,
        openai_gateway: Any,
        token_counter: TokenCounter,
        worker_id: str,
        job_lease_seconds: int = 60,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._openai_gateway = openai_gateway
        self._token_counter = token_counter
        self.worker_id = worker_id
        self._job_lease_seconds = job_lease_seconds
        self._stop = asyncio.Event()

    def install_signal_handlers(self) -> None:
        import signal

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:
                pass

    async def run_forever(self) -> None:
        logger.info("Job worker 启动: %s", self.worker_id)
        while not self._stop.is_set():
            try:
                await self._poll_once()
            except Exception:
                logger.exception("job worker 轮询周期异常")
            await asyncio.sleep(1.0)

    async def _poll_once(self) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                rows = await jobs_repo.claim_jobs(
                    session,
                    worker_id=self.worker_id,
                    lease_seconds=self._job_lease_seconds,
                    limit=5,
                )
        for row in rows:
            await self._execute_job(row)

    async def _execute_job(self, row: dict[str, Any]) -> None:
        job_type = row["job_type"]
        try:
            if job_type == "generate_title":
                await self._run_generate_title(row)
            elif job_type == "summarize_thread":
                await self._run_summarize_thread(row)
            elif job_type == "delete_thread":
                await self._run_delete_thread(row)
        except Exception as exc:
            logger.warning("Job 执行失败: type=%s job=%s err=%s", job_type, row["job_id"], exc)
            async with self._session_factory() as session:
                async with session.begin():
                    # P2（评审）：真实失败必须受 CONVERSATION_JOB_MAX_ATTEMPTS 约束，
                    # 达到上限转 dead_letter，不能无限重试。
                    max_attempts = int(getattr(self._config, "conversation_job_max_attempts", 10))
                    if int(row["attempt_count"]) >= max_attempts:
                        await jobs_repo.mark_job_dead_letter(
                            session,
                            row["job_id"],
                            worker_id=self.worker_id,
                            error_code=type(exc).__name__,
                        )
                    else:
                        await jobs_repo.mark_job_retry_wait(
                            session,
                            row["job_id"],
                            worker_id=self.worker_id,
                            error_code=type(exc).__name__,
                        )

    async def _run_generate_title(self, row: dict[str, Any]) -> None:
        """§7.6 标题：模型失败取首条用户消息前 20 字符兜底。"""
        thread_id = row["thread_id"]
        async with self._session_factory() as session:
            first = (
                (
                    await session.execute(
                        text(
                            "SELECT content FROM conversation.conversation_messages "
                            "WHERE thread_id = :thread_id AND role = 'user' "
                            "ORDER BY sequence LIMIT 1"
                        ),
                        {"thread_id": thread_id},
                    )
                )
                .mappings()
                .first()
            )
            fallback = (first["content"].strip()[:TITLE_FALLBACK_CHARS]) if first else "对话"
            messages = await self._load_recent_messages(session, thread_id, limit=50)
        title = fallback
        try:
            if messages:
                title = await self._openai_gateway.summarize_conversation(
                    messages=messages, previous_summary=None
                )
                title = title.strip()[:240] or fallback
        except Exception:
            logger.warning("标题模型失败，使用兜底标题")
            title = fallback
        async with self._session_factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "UPDATE conversation.conversation_threads "
                        "SET title = :title, updated_at = :now "
                        "WHERE thread_id = :thread_id"
                    ),
                    {"title": title, "now": datetime.now(UTC), "thread_id": thread_id},
                )
                await jobs_repo.mark_job_done(session, row["job_id"], worker_id=self.worker_id)

    async def _run_summarize_thread(self, row: dict[str, Any]) -> None:
        """§7.6 摘要：模型失败抛错进入重试/上限判定（评审 P2），不静默降级。

        Nit（第四轮评审）：以 Job 的 target_sequence 为摘要锚点——只摘要
        该锚点之后的消息（锚点 = 入队时的最新摘要序号），过期/重复 Job
        不再重读最新摘要造成内容重叠。
        """
        thread_id = row["thread_id"]
        anchor_seq = int(row.get("target_sequence") or 0)
        async with self._session_factory() as session:
            rows = await self._load_recent_messages(
                session, thread_id, limit=200, after_sequence=anchor_seq
            )
            latest_summary_seq = await self._latest_summary_sequence(session, thread_id)
        if not rows:
            # 无可摘要消息（锚点之后无新内容）：直接完成
            async with self._session_factory() as session:
                async with session.begin():
                    await jobs_repo.mark_job_done(session, row["job_id"], worker_id=self.worker_id)
            return
        summary = await self._openai_gateway.summarize_conversation(
            messages=rows, previous_summary=None
        )
        if not summary:
            raise RuntimeError("摘要模型返回空内容")
        async with self._session_factory() as session:
            async with session.begin():
                tokens = self._token_counter.count(summary)
                target_seq = max(latest_summary_seq or 0, anchor_seq)
                await session.execute(
                    text(
                        "INSERT INTO conversation.conversation_summaries ("
                        "  thread_id, sequence, content, token_count, created_at"
                        ") VALUES (:thread_id, :seq, :content, :tokens, now()) "
                        "ON CONFLICT (thread_id, sequence) DO UPDATE "
                        "SET content = EXCLUDED.content, token_count = EXCLUDED.token_count"
                    ),
                    {
                        "thread_id": thread_id,
                        "seq": target_seq + 1,
                        "content": summary,
                        "tokens": tokens,
                    },
                )
                await jobs_repo.mark_job_done(session, row["job_id"], worker_id=self.worker_id)

    async def _run_delete_thread(self, row: dict[str, Any]) -> None:
        """§8.6 / R3：deleting → deleted 协调（execute_delete_thread 承载状态机）。"""
        if row["deletion_generation"] is None:
            await self._fail(row, "MISSING_DELETION_GENERATION")
            return
        async with self._session_factory() as session:
            async with session.begin():
                result = await execute_delete_thread(
                    session,
                    job_id=row["job_id"],
                    thread_id=row["thread_id"],
                    deletion_generation=int(row["deletion_generation"]),
                    worker_id=self.worker_id,
                )
        logger.info("delete_thread 周期结果: %s thread=%s", result, row["thread_id"])

    async def _fail(self, row: dict[str, Any], error_code: str) -> None:
        async with self._session_factory() as session:
            async with session.begin():
                await jobs_repo.mark_job_dead_letter(
                    session, row["job_id"], worker_id=self.worker_id, error_code=error_code
                )

    async def _load_recent_messages(
        self,
        session: AsyncSession,
        thread_id: UUID,
        *,
        limit: int,
        after_sequence: int = 0,
    ) -> list[dict[str, Any]]:
        result = await session.execute(
            text(
                "SELECT role, content, sequence FROM conversation.conversation_messages "
                "WHERE thread_id = :thread_id AND status = 'completed' "
                "  AND sequence > :after_sequence "
                "ORDER BY sequence DESC LIMIT :limit"
            ),
            {"thread_id": thread_id, "limit": limit, "after_sequence": after_sequence},
        )
        rows = [dict(r) for r in result.mappings()]
        rows.reverse()
        return rows

    async def _latest_summary_sequence(self, session: AsyncSession, thread_id: UUID) -> int | None:
        result = await session.execute(
            text(
                "SELECT MAX(sequence) FROM conversation.conversation_summaries "
                "WHERE thread_id = :thread_id"
            ),
            {"thread_id": thread_id},
        )
        value = result.scalar_one_or_none()
        return int(value) if value is not None else None
