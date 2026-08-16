"""Study Memory Gateway（方案 §14，v1.2）。

- 通过 Memory Context API 读取 learner/mastery/图谱推荐（§4.3/§14），
  不跨域直连 Memory 数据库（§14.7）；
- 返回 (context, memory_context_hash)；不可用时抛 StudyMemoryUnavailableError，
  由节点降级（§16：personalization_status=degraded，不破坏既有计划）；
- Memory 内容只能作为模型数据注入，不能作为 system 指令（§14.1）。
"""

from __future__ import annotations

import logging
from typing import Any

from backend.memory.client import MemoryClient, MemoryClientError

#: Daily Feed 读取 Memory 的固定 query（写入与校验必须同一常量，评审必改）
FEED_MEMORY_QUERY = "今日学习推荐"

#: 指纹稳定字段子集（排除 query 回显、时间衰减 recommendations、
#: token_usage 与 truncated——这些字段每次组装都会变化，§14/评审必改）
STABLE_CONTEXT_FIELDS: tuple[str, ...] = ("learner", "mastery", "graph_states")


class StudyMemoryUnavailableError(RuntimeError):
    """Memory 读取不可用（§16：允许降级生成，不允许当作系统指令失败）。"""


class StudyMemoryGateway:
    """Study 域 Memory Gateway（组合 root 注入 MemoryClient，Conversation 同模式）。"""

    def __init__(
        self,
        *,
        client: MemoryClient,
        logger: logging.Logger | None = None,
    ) -> None:
        self._client = client
        self._logger = logger or logging.getLogger("study.gateways.memory")

    async def read_context(self, *, query: str, token_budget: int | None = None) -> dict[str, Any]:
        """读取学习上下文（§14：preferences/goals/plans/mastery/图谱推荐）。"""
        try:
            context = await self._client.build_learning_context(
                query=query, token_budget=token_budget
            )
        except MemoryClientError as exc:
            self._logger.warning(
                "Memory context 读取失败: code=%s http=%s", exc.code, exc.http_status
            )
            raise StudyMemoryUnavailableError(f"Memory 读取失败: {exc.code}") from exc
        except Exception as exc:
            self._logger.warning("Memory 连接异常: %s", type(exc).__name__)
            raise StudyMemoryUnavailableError("Memory 读取不可用") from exc
        return context.model_dump(mode="json")


def context_hash(context: dict[str, Any] | None) -> str | None:
    """Memory 输入快照哈希（§14.4：revision.memory_context_hash）。

    只覆盖稳定字段子集（learner/mastery/graph_states）；query 回显、
    时间衰减排序的 recommendations、token_usage、truncated 不参与——
    否则同一 Memory 状态每次组装哈希都不同（评审必改：指纹永不匹配）。
    """
    if context is None:
        return None
    import hashlib
    import json

    stable = {key: context[key] for key in STABLE_CONTEXT_FIELDS if key in context}
    canonical = json.dumps(
        stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
