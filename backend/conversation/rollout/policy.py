"""Rollout 持久化白名单与脱敏边界（memory-rebuild §1.5 / §5.3 Phase 1）。

对应 codex ``rollout/src/policy.rs::is_persisted_rollout_item``。原则是
**"小文本放全文、大对象放引用"**：消息文本 KB 级，全文廉价；evidence chunk 与向量
是 MB 级潜在体积，只存引用。

本模块回答"什么该落盘"；"一行长什么样"由 ``contracts/rollout.py`` 回答。两者分离的
原因：契约要长期稳定并进快照测试，白名单会随链路演进而调整。
"""

from __future__ import annotations

from typing import Any

from backend.conversation.contracts.rollout import (
    ROLLOUT_RECORD_TYPES,
    RolloutContractError,
    validate_rollout_payload,
)

#: Phase 1 写入的记录类型（§1.5 白名单表的 9 类）。
#:
#: 注意 §5.3 要求在 snapshot/memory/rewrite/evidence/answer/finalize 六个节点接入
#: recorder，但 §1.5 白名单**没有** memory 活动对应的类型——memory_status 已经内含在
#: ``turn_context_snapshot`` 里。因此 memory.py 与 answer.py 不产生独立记录，其产出
#: 分别经 snapshot 节点与 finalize 节点落盘（已登记于 memory-rebuild-deviations.md）。
PERSISTED_RECORD_TYPES: frozenset[str] = frozenset(
    {
        "thread_meta",
        "turn_started",
        "turn_completed",
        "turn_context_snapshot",
        "rewrite_plan",
        "evidence_set",
        "embedded_queries",
        "user_message",
        "assistant_message",
        # memory-rebuild §2.4（Phase 5）：prime 快照与记忆工具活动。
        # 这三种在 Phase 0 就已定型契约，Phase 5 起真正写入。
        "memory_prime",
        "memory_tool_call",
        "memory_tool_result",
    }
)

#: 仍然保留、尚未有任何写入方的记录类型（当前为空）。
#:
#: Phase 0 曾把 memory_prime / memory_tool_call / memory_tool_result 放在这里；
#: Phase 5 实现后它们已进入 PERSISTED_RECORD_TYPES。保留本集合是为了让
#: "契约已定型但实现未落地"的类型有统一的登记处，避免散落在注释里。
RESERVED_RECORD_TYPES: frozenset[str] = frozenset()

#: 明确**不落盘**的瞬态物（§1.5 白名单表第三列）。
#:
#: 它们本来就不是合法记录类型（构造 ``RolloutRecord`` 会被 Literal 拒绝），这里显式
#: 列出是为了让拒绝原因可读：命中时报的是"这是瞬态传输态，按设计不落盘"，而不是
#: 泛泛的"未知类型"。
TRANSIENT_ITEM_NAMES: frozenset[str] = frozenset(
    {
        "answer_delta",  # SSE delta / answer_buffer 流式中间态
        "answer_buffer",
        "cancel_token",  # 取消令牌
        "lease",  # lease / fencing 等瞬态控制
        "gateway_http",  # gateway 原始 HTTP 细节
        "http_request",
        "http_response",
    }
)

#: 禁止出现在任何落盘 payload 中的键名（小写比较）。
#:
#: 契约的 payload 模型都是 ``extra="forbid"`` 的固定形状，唯一能夹带自由结构的是
#: ``rewrite_plan.plan`` 这类 ``dict[str, Any]`` 字段；凭证/签名一旦落到 JSONL 就绕过
#: 了"凭据只从 secret 注入"的约束，因此在写入口做一次递归键扫描。
FORBIDDEN_PAYLOAD_KEYS: frozenset[str] = frozenset(
    {
        "authorization",
        "access_key",
        "secret_key",
        "api_key",
        "password",
        "token",
        "signature",
        "signed_url",
        "presign_url",
        "cookie",
        "credential",
        "credentials",
    }
)


class RolloutPolicyError(RolloutContractError):
    """记录被白名单或脱敏边界拒绝。"""


def should_persist(record_type: str) -> bool:
    """该记录类型是否属于 Phase 1 落盘白名单。"""
    return record_type in PERSISTED_RECORD_TYPES


def ensure_persistable(record_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    """白名单 + 契约 + 脱敏三重校验，返回规范化后的 payload。

    顺序有意为之：先判白名单给出可读拒绝原因，再走契约校验，最后扫凭证键。
    """
    if record_type in TRANSIENT_ITEM_NAMES:
        raise RolloutPolicyError(f"{record_type} 是瞬态传输态，按 §1.5 不落盘")
    if record_type in RESERVED_RECORD_TYPES:
        raise RolloutPolicyError(f"{record_type} 契约已定型但尚无实现，暂不写入")
    if record_type not in ROLLOUT_RECORD_TYPES:
        raise RolloutPolicyError(f"未知持久化记录类型: {record_type}")
    if not should_persist(record_type):
        raise RolloutPolicyError(f"{record_type} 不在 Phase 1 落盘白名单")
    try:
        normalized = validate_rollout_payload(record_type, payload)
    except RolloutContractError as exc:
        # 统一在策略边界暴露一种错误类型：调用方只 catch RolloutPolicyError 即可，
        # 不必同时知道契约层的异常（RolloutPolicyError 本身是它的子类，不丢语义）。
        raise RolloutPolicyError(f"{record_type} payload 不符合契约: {exc}") from exc
    _reject_forbidden_keys(record_type, normalized)
    return normalized


def _reject_forbidden_keys(record_type: str, value: Any, *, path: str = "") -> None:
    """递归扫描凭证类键名；命中即拒绝整条记录。"""
    if isinstance(value, dict):
        for key, item in value.items():
            key_text = str(key).lower()
            if key_text in FORBIDDEN_PAYLOAD_KEYS:
                location = f"{path}.{key}" if path else str(key)
                raise RolloutPolicyError(f"{record_type} payload 含禁止落盘的凭证类字段 {location}")
            _reject_forbidden_keys(record_type, item, path=f"{path}.{key}" if path else str(key))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_forbidden_keys(record_type, item, path=f"{path}[{index}]")
