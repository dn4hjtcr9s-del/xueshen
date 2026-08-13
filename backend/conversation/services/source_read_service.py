"""ConversationSourceReadService：生产 ConversationReader 的实现（方案 §8，P0 闭环）。

职责：
- 只按请求中的 message_ids 精确读取，不扩展为整个线程（§8.3 #3）；
- 校验线程属于目标用户、消息已完成、eligible_for_memory=true 且未删除（§8.3）；
- checkpoint_id 非空时校验消息在来源快照中已提交且版本匹配（§8.3 #5）；
- 按 sequence 稳定排序，按 source_version/thread_id/turn_id 组装 SourceItem（§8.4）；
- 返回 SourceBundle 前执行冻结契约：去重后 ≤80,000 UTF-8 bytes、
  单项 ≤20,000 字符、≤200 items、metadata ≤4096 bytes/≤50 keys（§8.3 #10）；
- 消息已删除返回 SOURCE_DELETED；不存在/归属不符/版本不匹配统一 SOURCE_NOT_FOUND
  （§7.2，内部日志记录细分原因）。
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.conversation.persistence import messages as messages_repo
from backend.conversation.persistence import threads as threads_repo
from backend.memory.contracts.errors import (
    SourceDeletedError,
    SourceNotFoundError,
    SourceTooLargeError,
)
from backend.memory.contracts.evidence import SourceBundle, SourceItem


class ConversationSourceReadService:
    """Source read 端口适配器（内部 HTTP 端点与 Memory 侧 Http 客户端背后的实现）。"""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        logger: logging.Logger | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._logger = logger or logging.getLogger("conversation.reader")

    async def read_source_bundle(
        self,
        *,
        user_id: UUID,
        thread_id: str,
        checkpoint_id: str | None,
        message_ids: list[str],
    ) -> SourceBundle:
        """读取并校验 SourceBundle（§8.3）。"""
        try:
            parsed_thread = UUID(thread_id)
        except ValueError as exc:
            raise SourceNotFoundError("来源不存在或无权访问") from exc
        parsed_ids = self._parse_message_ids(message_ids)

        async with self._session_factory() as session:
            thread = await threads_repo.get_thread(session, parsed_thread)
            if thread is None or thread["status"] in ("deleting", "deleted"):
                # 删除中/已删除统一按不存在处理；内部记录细分原因
                self._logger.info("reader: thread missing or deleting/deleted: %s", parsed_thread)
                raise SourceNotFoundError("来源不存在或无权访问")
            if thread["user_id"] != user_id:
                self._logger.warning("reader: thread ownership mismatch: %s", parsed_thread)
                raise SourceNotFoundError("来源不存在或无权访问")

            # P2（评审）：include_deleted=True 以区分"已删除"与"不存在/未完成"——
            # 已删除消息按 §7.2 冻结语义返回 SOURCE_DELETED。
            rows = await messages_repo.get_messages_by_ids(
                session, parsed_ids, include_deleted=True
            )
            by_id = {row["message_id"]: row for row in rows}

            if len(by_id) != len(parsed_ids):
                missing = [mid for mid in parsed_ids if mid not in by_id]
                self._logger.info("reader: message not found: %s", missing)
                raise SourceNotFoundError("来源不存在或无权访问")

            items: list[SourceItem] = []
            for message_id in parsed_ids:
                row = by_id[message_id]
                if row["thread_id"] != parsed_thread or row["user_id"] != user_id:
                    self._logger.warning("reader: message ownership mismatch: %s", message_id)
                    raise SourceNotFoundError("来源不存在或无权访问")
                if row["status"] == "deleted":
                    # §7.2 / 评审 P2：消息已删除 → SOURCE_DELETED（区别于不存在）
                    self._logger.info("reader: message deleted: %s", message_id)
                    raise SourceDeletedError("来源已被删除")
                if row["status"] != "completed":
                    # 未完成消息不可读（§8.3 #1/#9）
                    raise SourceNotFoundError("来源不存在或无权访问")
                if not row["eligible_for_memory"]:
                    raise SourceNotFoundError("来源不存在或无权访问")
                items.append(
                    SourceItem(
                        source_ref=f"conversation:{thread_id}:message:{message_id}",
                        role="assistant" if row["role"] == "assistant" else "user",
                        content=row["content"],
                        occurred_at=row["occurred_at"],
                        metadata={
                            "source_version": row["content_hash"],
                            "thread_id": str(parsed_thread),
                            "turn_id": str(row["turn_id"]),
                            "sequence": int(row["sequence"]),
                        },
                    )
                )
            if checkpoint_id is not None:
                # §7.2 / D9：Reader 按全部读取消息重算 canonical manifest 后完全匹配
                if not self._checkpoint_matches(
                    checkpoint_id=checkpoint_id,
                    thread_id=parsed_thread,
                    rows=[by_id[mid] for mid in parsed_ids],
                ):
                    self._logger.info(
                        "reader: source checkpoint mismatch for thread %s", parsed_thread
                    )
                    raise SourceNotFoundError("来源不存在或无权访问")
        # 按 sequence 稳定排序（§8.3 #4）
        items.sort(key=lambda item: int(item.metadata.get("sequence", 0)))
        try:
            return SourceBundle.from_items(items)
        except SourceTooLargeError:
            raise
        except ValueError as exc:
            raise SourceTooLargeError(str(exc)) from exc

    def _checkpoint_matches(
        self,
        *,
        checkpoint_id: str,
        thread_id: UUID,
        rows: list[dict[str, Any]],
    ) -> bool:
        """source_checkpoint_id 完整性校验（§7.2 / D9）。

        finalize 与 Reader 共用 build_source_manifest 同一算法，完整 SHA-256
        必须完全匹配（禁止截取前 16 位）。
        """
        from backend.conversation.contracts.domain import build_source_manifest

        turn_id = rows[0]["turn_id"]
        expected = build_source_manifest(thread_id, turn_id, rows)
        return checkpoint_id == expected

    @staticmethod
    def _parse_message_ids(message_ids: list[str]) -> list[UUID]:
        parsed: list[UUID] = []
        for raw in message_ids:
            try:
                parsed.append(UUID(str(raw)))
            except ValueError as exc:
                raise SourceNotFoundError("来源不存在或无权访问") from exc
        return parsed
