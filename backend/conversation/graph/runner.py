"""ConversationGraph 构建器与 Runner（方案 §5.2 / §10 / 附录 A.3）。

- 同一张编译图；Feature Flag 在路由函数里读 runtime context 的 flag 快照
  （附录 A.10：不编译两张图）；
- graph_thread_id = "conv-turn:{turn_id}" 确定性派生（附录 A.3）；
- 恢复决策树：① 有 checkpoint → resume；② 无 → 从 START 新跑；
  ③ checkpoint 反序列化失败 → 记 checkpoint_recovery_failed 指标并从 START 重跑。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from backend.conversation.contracts.graph import ConversationGraphInput
from backend.conversation.graph.state import ConversationRuntimeContext


class ConversationGraphRunner:
    """Graph 执行入口（worker claim 后调用，§5.4 / 附录 A.3）。"""

    def __init__(
        self,
        *,
        compiled_graph: Any,
        runtime_context: ConversationRuntimeContext,
        graph_thread_id_for_turn: Callable[[UUID], str],
        logger: logging.Logger | None = None,
    ) -> None:
        self._compiled = compiled_graph
        self._runtime = runtime_context
        self._graph_thread_id_for_turn = graph_thread_id_for_turn
        self._logger = logger or logging.getLogger("conversation.graph")

    def graph_thread_id(self, turn_id: UUID) -> str:
        return self._graph_thread_id_for_turn(turn_id)

    async def execute_turn(self, turn: dict[str, Any], *, worker_id: str) -> None:
        """执行/恢复 Turn（附录 A.3 恢复决策树）。

        turn 行已由 worker claim（status=running, lease 已写入）。
        Graph 内部通过 finalize 完成终态与 fencing。

        首次执行时以 ConversationGraphInput 作为初始输入；
        恢复时传 None 由 checkpointer 自取最新 checkpoint（附录 A.3）。
        """
        graph_thread_id = self.graph_thread_id(turn["turn_id"])
        has_checkpoint = await self._has_checkpoint(graph_thread_id)
        graph_input: dict[str, Any] | None = None
        if not has_checkpoint:
            graph_input = ConversationGraphInput(
                user_id=turn["user_id"],
                thread_id=turn["thread_id"],
                turn_id=turn["turn_id"],
                user_message_id=turn["user_message_id"],
                request_id=turn["request_id"],
                run_id=turn["run_id"],
                expected_thread_version=turn["expected_thread_version"],
            ).model_dump(mode="json")
        # runtime 通过注入器传入 graph（LangGraph 的 config 传递）
        config: dict[str, Any] = {
            "configurable": {"thread_id": graph_thread_id, "runtime": self._runtime}
        }
        try:
            await self._compiled.ainvoke(graph_input, config=config)
        except Exception:
            self._logger.exception("Graph 执行失败: turn_id=%s", turn["turn_id"])
            raise

    async def _has_checkpoint(self, graph_thread_id: str) -> bool:
        """附录 A.3 决策树 ①/②：该 thread 是否存在 checkpoint。"""
        try:
            checkpointer = getattr(self._compiled, "checkpointer", None)
            if checkpointer is None:
                return False
            checkpoint_tuple = await checkpointer.aget_tuple(
                {"configurable": {"thread_id": graph_thread_id}}
            )
            return checkpoint_tuple is not None
        except Exception:
            self._logger.warning("checkpoint 查询失败，按无 checkpoint 处理: %s", graph_thread_id)
            return False
