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
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from backend.conversation.contracts.graph import ConversationGraphInput
from backend.conversation.graph.state import ConversationRuntimeContext
from backend.conversation.rollout.recorder import record_rollout


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
        recorder = getattr(self._runtime, "rollout_recorder", None)
        # memory-rebuild §5.3：turn 边界即段边界。open 失败/降级时 recorder 返回 None，
        # 后续 record_rollout 一律 no-op，不影响本 turn 执行。
        if recorder is not None:
            await self._open_rollout_segment(recorder, turn, worker_id=worker_id)
        try:
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
            await self._compiled.ainvoke(graph_input, config=config)
        except Exception:
            self._logger.exception("Graph 执行失败: turn_id=%s", turn["turn_id"])
            if recorder is not None:
                await self._record_turn_failed(turn)
            raise
        finally:
            if recorder is not None:
                # §1.5："finalize 后 flush() 等 ack"，再结束本轮 recorder 生命周期。
                # close_turn 内部先等 flush ack 再释放句柄。
                await recorder.close_turn()

    async def _open_rollout_segment(
        self, recorder: Any, turn: dict[str, Any], *, worker_id: str
    ) -> None:
        """开启本 turn 的 rollout 段。

        ``thread_created_at`` 用于段目录分片（§1.5 时间戳① = thread 创建时间），它属于
        thread 行而非 turn 行。claim 查询已 JOIN 出 ``thread_created_at``；缺失时
        （单测或其它入口直接构造 turn dict）退化为当前时间——只影响分片目录的选择，
        不影响 ordinal 单调性与记录内容。
        """
        created_at = turn.get("thread_created_at")
        if isinstance(created_at, datetime):
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
        else:
            created_at = self._runtime.clock.now()
        # fencing：封存写 manifest 时要复核 (lease_owner, lease_generation)，
        # 失租的 worker 不得改 manifest（与 finalize 的 fencing 同源）。
        lease_generation = turn.get("lease_generation")
        fence = (worker_id, int(lease_generation)) if lease_generation is not None else None
        await recorder.open_turn(
            thread_id=turn["thread_id"],
            turn_id=turn["turn_id"],
            user_id=turn["user_id"],
            thread_created_at=created_at,
            fence=fence,
        )

    async def _record_turn_failed(self, turn: dict[str, Any]) -> None:
        """异常路径补写 turn_completed(status=failed)。

        §5.3 要求 turn_completed 必须带 status 与 degraded flags；正常路径由 finalize
        节点写入，图在 finalize 之前失败时若不补写，段里就没有终态记录。
        recorder 侧保证一个段最多一条 turn_completed，因此 finalize 已成功后再异常
        不会被写成"完成又失败"。
        """
        await record_rollout(
            self._runtime,
            "turn_completed",
            {
                "turn_id": str(turn["turn_id"]),
                "status": "failed",
                "degraded_flags": ["graph_failed"],
                "completed_at": self._runtime.clock.now().isoformat(),
            },
        )

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
