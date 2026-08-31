"""Conversation Graph 可观察进度事件。

这里只公开可验证的流水线阶段、状态和统计摘要，不暴露模型隐藏推理、系统提示词或
内部思维链。进度事件属于增强体验，写入失败时不得中断回答主链路。
"""

from __future__ import annotations

from typing import Any

from backend.conversation.contracts.api import ProgressStage, ProgressStatus
from backend.conversation.contracts.events import TurnEventWrite
from backend.conversation.graph.state import ConversationRuntimeContext


async def emit_progress(
    runtime: ConversationRuntimeContext,
    state: dict[str, Any],
    *,
    stage: ProgressStage,
    status: ProgressStatus,
    title: str,
    detail: str | None = None,
    metadata: dict[str, str | int | float | bool | None] | None = None,
) -> None:
    """最佳努力写入 ``turn.progress``，供前端展示安全的执行阶段摘要。"""

    repo = runtime.conversation_repository
    if repo is None or repo.session_factory is None or not state.get("turn_id"):
        return
    payload: dict[str, object] = {
        "stage": stage,
        "status": status,
        "title": title,
        "metadata": metadata or {},
    }
    if detail:
        payload["detail"] = detail[:500]
    try:
        async with repo.session_factory() as session:
            async with session.begin():
                await runtime.turn_event_writer.append(
                    session,
                    write=TurnEventWrite(
                        turn_id=state["turn_id"],
                        event_type="turn.progress",
                        request_id=str(state.get("request_id") or ""),
                        run_id=str(state.get("run_id") or ""),
                        payload=payload,
                    ),
                )
    except Exception:
        # 进度可视化是旁路能力，数据库瞬时错误不能让一次本可完成的回答失败。
        runtime.logger.warning(
            "Conversation 进度事件写入失败: stage=%s status=%s",
            stage,
            status,
            exc_info=True,
        )
