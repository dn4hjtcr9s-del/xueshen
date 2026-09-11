"""批量总结分支的纯逻辑单测（memory-rebuild §4.2 / §5.8 Phase 6）。

图级行为（逐成员提交、重跑不重复写、成员状态回写）由
``tests/integration/test_memory_batch_graph.py`` 用真实库覆盖；这里只测条件边路由与
纯函数，保证"循环一定能收敛"这件事在无数据库时也能被证明。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from backend.memory.graph.batch import (
    MAX_BATCH_WARNINGS,
    _cap_warnings,
    _declared_member_ids,
    _operation_from_row,
    route_after_begin_member,
    route_after_record_member,
    route_after_summary_finalize,
)


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "batch_active": True,
        "batch_members": [{"operation_id": "a"}, {"operation_id": "b"}],
        "batch_index": 0,
        "batch_selected": {},
        "batch_warnings": [],
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


def test_route_after_summary_finalize_only_loops_in_batch_mode() -> None:
    assert route_after_summary_finalize({"batch_active": True}) == "batch_member_done"
    # 单条证据路径（batch_active 缺失/为假）行为与改造前一致
    assert route_after_summary_finalize({}) == "normalize"
    assert route_after_summary_finalize({"batch_active": False}) == "normalize"


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
