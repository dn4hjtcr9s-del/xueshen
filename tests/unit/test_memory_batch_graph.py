"""批量总结分支的纯逻辑单测（memory-rebuild §4.2 / §5.8 Phase 6）。

图级行为（逐成员提交、重跑不重复写、成员状态回写）由
``tests/integration/test_memory_batch_graph.py`` 用真实库覆盖；这里只测条件边路由与
纯函数，保证"循环一定能收敛"这件事在无数据库时也能被证明。

评审新发现 16：``batch.route_after_summary_finalize`` 已删除（生产代码里没有调用点，
批量链的收尾路由统一由 ``runner._route_after_finalize_summary_result`` 承担），
对应断言搬到 ``tests/unit/test_member_failure_isolation.py``——那里测的是**真正生效**
的那个路由函数。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from backend.memory.graph import batch as batch_module
from backend.memory.graph.batch import (
    MAX_BATCH_WARNINGS,
    _cap_warnings,
    _declared_member_ids,
    _operation_from_row,
    route_after_begin_member,
    route_after_record_member,
)


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "batch_active": True,
        "batch_members": [{"operation_id": "a"}, {"operation_id": "b"}],
        "batch_index": 0,
        "batch_selected": {},
        "batch_warnings": [],
        # finalize_batch_result 会把批次 operation 还原回 state["operation"]
        "batch_operation": {"operation_id": "batch-1"},
        "operation": {"operation_id": "batch-1"},
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------------------
# 路由：循环必须收敛
# ---------------------------------------------------------------------------


def test_route_after_begin_member_prefers_summary_chain() -> None:
    assert route_after_begin_member(_state(batch_selected={"operation_id": "a"})) == "member"


def test_route_after_begin_member_loops_past_skipped_members() -> None:
    # 选中为空但还有成员没走过：说明上一条被跳过，继续推进指针
    assert route_after_begin_member(_state(batch_index=1)) == "next"
    # 指针走到底 → 进入 consolidation
    assert route_after_begin_member(_state(batch_index=2)) == "consolidate"


def test_route_after_begin_member_handles_empty_batch() -> None:
    assert route_after_begin_member(_state(batch_members=[], batch_index=0)) == "consolidate"


def test_route_after_record_member_loops_until_exhausted() -> None:
    assert route_after_record_member(_state(batch_index=1)) == "next"
    assert route_after_record_member(_state(batch_index=2)) == "consolidate"
    assert route_after_record_member(_state(batch_members=[], batch_index=0)) == "consolidate"


def test_route_after_summary_finalize_is_gone() -> None:
    """评审新发现 16：死路由已删除；生效的实现只在 runner 那一份。"""
    assert not hasattr(batch_module, "route_after_summary_finalize")

    from backend.memory.graph.runner import _route_after_finalize_summary_result

    # 真正被装配进图的那份路由：批量模式回成员循环，单条路径语义不变
    assert _route_after_finalize_summary_result({"batch_active": True}) == "batch_member_done"
    assert _route_after_finalize_summary_result({}) == "continue"
    assert _route_after_finalize_summary_result({"batch_active": False}) == "continue"


# ---------------------------------------------------------------------------
# 批次级失败信号（评审新发现 12）：全部成员被隔离时不得报 succeeded
# ---------------------------------------------------------------------------


class _ReleaseStub:
    """替代 ``_release_failed_members``：只记录被释放的成员，不碰数据库。"""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []

    async def __call__(self, runtime: Any, failed: list[dict[str, Any]]) -> Any:
        self.calls.append(list(failed))
        return batch_module._ReleaseSummary(released=len(failed), dead_lettered=0)


def _failed_entry(operation_id: str, *, reason: str = "member_exception") -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "outcome": batch_module.MEMBER_OUTCOME_FAILED,
        "reason": reason,
        "failed_node": "extract_candidates",
        "error_type": "OpenAISchemaInvalidError",
        "error_message": "模型输出无法解析",
    }


@pytest.mark.asyncio
async def test_finalize_raises_dead_letter_signal_when_no_member_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一条都没写、也没有审核候选 → 批次级失败（dead_letter），不是 succeeded。"""
    stub = _ReleaseStub()
    monkeypatch.setattr(batch_module, "_release_failed_members", stub)
    state = _state(
        batch_members=[{"operation_id": "a"}, {"operation_id": "b"}],
        batch_processed=[
            {
                "operation_id": "a",
                "outcome": batch_module.MEMBER_OUTCOME_FAILED,
                "mutations": [],
                "review_candidate_ids": [],
            },
            {
                "operation_id": "b",
                "outcome": batch_module.MEMBER_OUTCOME_FAILED,
                "mutations": [],
                "review_candidate_ids": [],
            },
        ],
        batch_failed=[_failed_entry("a"), _failed_entry("b")],
        batch_warnings=[],
    )
    with pytest.raises(batch_module.BatchAllMembersFailedError) as excinfo:
        await batch_module.finalize_batch_result(state, None)

    # 失败成员仍先被释放回证据池（重试出口不能因为批次判死而丢）
    assert len(stub.calls) == 1
    assert [e["operation_id"] for e in stub.calls[0]] == ["a", "b"]
    # 结构化信号：这是 classify_failure 会判 DEAD_LETTER 的域错误，且 code 可区分
    from backend.memory.contracts.errors import MemoryError
    from backend.memory.worker.retry import FailureAction, classify_failure

    assert isinstance(excinfo.value, MemoryError)
    assert excinfo.value.code == batch_module.BATCH_ALL_MEMBERS_FAILED
    assert classify_failure(excinfo.value) is FailureAction.DEAD_LETTER
    # 公开可见面（public_error.message）必须带上人数与失败摘要
    message = str(excinfo.value)
    assert "2/2" in message
    assert "OpenAISchemaInvalidError" in message


@pytest.mark.asyncio
async def test_finalize_stays_succeeded_when_any_member_wrote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§5.8：单成员失败不拖垮整批 —— 只要有一条写入，批次照常 succeeded。"""
    monkeypatch.setattr(batch_module, "_release_failed_members", _ReleaseStub())
    state = _state(
        batch_processed=[
            {
                "operation_id": "a",
                "outcome": batch_module.MEMBER_OUTCOME_FAILED,
                "mutations": [],
                "review_candidate_ids": [],
            },
            {
                "operation_id": "b",
                "outcome": batch_module.MEMBER_OUTCOME_SUCCEEDED,
                "mutations": [{"mutation_id": "m1"}],
                "review_candidate_ids": [],
            },
        ],
        batch_failed=[_failed_entry("a")],
        batch_warnings=[],
    )
    result = await batch_module.finalize_batch_result(state, None)
    assert result["commit_result"]["mutations"] == [{"mutation_id": "m1"}]
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_finalize_does_not_fail_batch_for_review_candidates_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全是审核候选（needs_review）时交回 normalize_result 判 needs_review，不抛错。"""
    monkeypatch.setattr(batch_module, "_release_failed_members", _ReleaseStub())
    state = _state(
        batch_processed=[
            {
                "operation_id": "a",
                "outcome": batch_module.MEMBER_OUTCOME_NEEDS_REVIEW,
                "mutations": [],
                "review_candidate_ids": ["c1"],
            }
        ],
        batch_failed=[{"operation_id": "a", "outcome": batch_module.MEMBER_OUTCOME_NEEDS_REVIEW}],
        batch_warnings=[],
    )
    result = await batch_module.finalize_batch_result(state, None)
    assert result["commit_result"]["review_candidate_ids"] == ["c1"]
    assert result["commit_result"]["mutations"] == []


@pytest.mark.asyncio
async def test_finalize_empty_batch_stays_succeeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """没有任何成员（或全部 no_change）时不是失败：批次照常 succeeded。"""
    monkeypatch.setattr(batch_module, "_release_failed_members", _ReleaseStub())
    result = await batch_module.finalize_batch_result(
        _state(batch_members=[], batch_processed=[], batch_failed=[], batch_warnings=[]), None
    )
    assert result["commit_result"]["mutations"] == []
    assert result["errors"] == []


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def test_declared_member_ids_reads_payload_contract() -> None:
    from backend.memory.contracts.commands import SummarizeUserMemoryBatchCommand
    from backend.memory.contracts.operations import MemoryOperation

    member_ids = [uuid4(), uuid4()]
    operation_id = uuid4()
    operation = MemoryOperation(
        operation_id=operation_id,
        idempotency_key="batch-1",
        user_id=uuid4(),
        actor_type="system",
        input_kind="evidence",
        operation_type="summarize_user_memory_batch",
        priority=50,
        occurred_at=datetime.now(UTC),
        payload=SummarizeUserMemoryBatchCommand(
            target_user_id=uuid4(),
            batch_operation_id=operation_id,
            member_operation_ids=member_ids,
        ),
        trace_id="t" * 32,
        graph_thread_id="memory-op:x",
    )

    assert _declared_member_ids(operation) == {str(item) for item in member_ids}


def test_operation_from_row_keeps_only_contract_fields() -> None:
    """DB 行含状态/Lease 等额外列，投影必须剔除（MemoryOperation extra='forbid'）。"""
    operation_id = uuid4()
    row = {
        "operation_id": operation_id,
        "idempotency_key": "k",
        "user_id": uuid4(),
        "actor_type": "system",
        "input_kind": "evidence",
        "operation_type": "conversation_evidence",
        "priority": 50,
        "occurred_at": datetime.now(UTC),
        "payload": {
            "kind": "conversation_evidence",
            "thread_id": "t",
            "message_ids": ["m1"],
            "trigger": "turn_boundary",
        },
        "trace_id": "t" * 32,
        "graph_thread_id": "memory-op:y",
        # 以下都是 DB 额外列，必须被剔除
        "status": "pending_batch",
        "locked_by": "worker-1",
        "lease_generation": 3,
        "batch_operation_id": str(uuid4()),
        "result": None,
    }
    projected = _operation_from_row(row)

    assert "status" not in projected
    assert "locked_by" not in projected
    assert "batch_operation_id" not in projected
    from backend.memory.contracts.operations import MemoryOperation

    # 能通过契约校验（否则加载成员时就会炸）
    assert MemoryOperation.model_validate(projected).operation_id == operation_id


def test_cap_warnings_dedupes_and_truncates() -> None:
    warnings = [f"w{i}" for i in range(MAX_BATCH_WARNINGS + 5)]
    capped = _cap_warnings([*warnings, "w0"])  # 重复项应被去掉

    assert len(capped) == MAX_BATCH_WARNINGS + 1
    assert capped[0] == "w0"
    assert capped[-1] == "其余 5 条警告已省略"


def test_cap_warnings_keeps_small_lists_intact() -> None:
    assert _cap_warnings(["a", "b", "a"]) == ["a", "b"]
