"""成员级失败隔离的纯逻辑单测（评审 I-9 / C-2）。

这里不碰数据库：只验证守卫（``runner._guard_member_node``）与条件边路由的**判定表**，
真实写入行为由 ``tests/integration/test_memory_batch_worker_e2e.py`` 用真实 Worker 覆盖。
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.memory.contracts.errors import (
    DatabaseUnavailableError,
    LeaseFencedError,
    OpenAISchemaInvalidError,
)
from backend.memory.graph import batch as batch_module
from backend.memory.graph.runner import (
    _guard_member_node,
    _route_after_finalize_summary_result,
    _route_after_member_node,
    _route_after_route_candidates,
)
from backend.memory.graph.state import MemoryManagerState


async def _ok(state: MemoryManagerState, runtime: Any) -> dict[str, Any]:
    return {"warnings": ["ok"]}


def _guarded(raiser: Any) -> Any:
    return _guard_member_node(raiser)


# ---------------------------------------------------------------------------
# 守卫：什么被隔离、什么必须上抛
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_isolates_member_exception_in_batch_mode() -> None:
    """批量分支：非致命异常变成 batch_member_error（稳定 reason + 截断摘要 + 节点名）。"""

    async def raiser(state: MemoryManagerState, runtime: Any) -> dict[str, Any]:
        raise OpenAISchemaInvalidError("模型输出无法解析" + "x" * 500)

    result = await _guarded(raiser)({"batch_active": True}, None)
    error = result["batch_member_error"]
    assert error["reason"] == batch_module.MEMBER_FAILURE_REASON
    assert error["error_type"] == "OpenAISchemaInvalidError"
    assert error["node"] == "raiser"
    assert len(error["message"]) == batch_module.MAX_FAILURE_MESSAGE_CHARS


@pytest.mark.asyncio
async def test_guard_reraises_in_single_operation_mode() -> None:
    """单条路径（batch_active 为假/缺失）：异常原样上抛，行为与改造前逐字一致。"""

    async def raiser(state: MemoryManagerState, runtime: Any) -> dict[str, Any]:
        raise OpenAISchemaInvalidError("单条路径必须上抛")

    for state in ({}, {"batch_active": False}):
        with pytest.raises(OpenAISchemaInvalidError):
            await _guarded(raiser)(state, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        DatabaseUnavailableError("数据库连接失效"),
        ConnectionError("连接被重置"),
        TimeoutError("超时"),
        LeaseFencedError("Lease 已易主"),
    ],
)
async def test_guard_reraises_environment_failures_even_in_batch_mode(exc: BaseException) -> None:
    """环境不可用 / 失租信号不隔离：换一条成员重试同样会失败，必须交给 Worker 语义。"""

    async def raiser(state: MemoryManagerState, runtime: Any) -> dict[str, Any]:
        raise exc

    with pytest.raises(type(exc)):
        await _guarded(raiser)({"batch_active": True}, None)


@pytest.mark.asyncio
async def test_guard_clears_stale_error_on_success() -> None:
    """成功的一步必须清掉失败信号，否则会误触发后续节点的短路条件边。"""
    result = await _guarded(_ok)({"batch_active": True, "batch_member_error": {"a": 1}}, None)
    assert result["batch_member_error"] == {}
    assert result["warnings"] == ["ok"]


def test_is_member_fatal_classification() -> None:
    assert batch_module.is_member_fatal(DatabaseUnavailableError("x")) is True
    assert batch_module.is_member_fatal(OpenAISchemaInvalidError("x")) is False
    assert batch_module.is_member_fatal(ValueError("x")) is False


# ---------------------------------------------------------------------------
# 条件边路由：失败信号优先于链自身的分支
# ---------------------------------------------------------------------------


def test_route_after_member_node_prefers_failure_signal() -> None:
    assert _route_after_member_node({"batch_member_error": {"reason": "x"}}) == "member_failed"
    assert _route_after_member_node({}) == "continue"
    assert _route_after_member_node({"batch_member_error": {}}) == "continue"


def test_route_after_route_candidates_keeps_both_chain_semantics() -> None:
    # 失败信号优先
    assert (
        _route_after_route_candidates({"batch_member_error": {"a": 1}, "route": "summary_process"})
        == "member_failed"
    )
    # 单条路径的 route 语义逐字不变：无候选才去 finalize，否则落到"继续"。
    # （runner 里 `continue`（单条链的同名节点）与 `summary_process`（批量链的守卫版节点）
    #  都映射到 persist_review_candidates，因此两者行为等价。）
    assert _route_after_route_candidates({"route": "summary_process"}) == "continue"
    # 单条路径无候选 → finalize_summary_result
    assert _route_after_route_candidates({"route": "summary_finalize"}) == "summary_finalize"
    assert _route_after_route_candidates({}) == "summary_finalize"
    # 批量链的继续分支（守卫版 persist_review_candidates）
    assert (
        _route_after_route_candidates({"batch_active": True, "route": "summary_process"})
        == "summary_process"
    )


def test_route_after_finalize_summary_result_keeps_both_chain_semantics() -> None:
    assert _route_after_finalize_summary_result({"batch_active": True}) == "batch_member_done"
    assert (
        _route_after_finalize_summary_result({"batch_active": True, "batch_member_error": {"a": 1}})
        == "member_failed"
    )
    assert _route_after_finalize_summary_result({}) == "continue"
    assert _route_after_finalize_summary_result({"batch_active": False}) == "continue"
