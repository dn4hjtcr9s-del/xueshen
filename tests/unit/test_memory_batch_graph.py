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
# errors-only 成员的终态语义（复审遗留 DEV-072）：真值表 + 释放 + 可见性
# ---------------------------------------------------------------------------


def _member_state(**overrides: Any) -> dict[str, Any]:
    """``record_batch_member`` 的输入 state（只关心成员级判定的那几个键）。"""
    state: dict[str, Any] = {
        "batch_selected": {"operation_id": "a"},
        "batch_processed": [],
        "batch_failed": [],
        "batch_warnings": [],
        "warnings": [],
        "batch_member_error": {},
        "commit_result": {},
        "review_candidates": [],
        "errors": [],
        "llm_call_count": 0,
        "batch_llm_call_count": 0,
    }
    state.update(overrides)
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("member_error", "mutations", "review_ids", "errors", "expected", "in_failed", "released"),
    [
        # 异常隔离：没有写入，走释放路径
        ({"reason": "member_exception"}, [], [], [], "failed", True, True),
        # 真的写进去了：不算"未直接写入"（哪怕过程中有 errors）
        ({}, [{"mutation_id": "m1"}], [], [], "succeeded", False, False),
        (
            {},
            [{"mutation_id": "m1"}],
            [],
            [{"code": "LLM_BUDGET_EXHAUSTED"}],
            "succeeded",
            False,
            False,
        ),
        # 有审核候选：人工结论优先，不释放
        ({}, [], [{"candidate_id": "c1"}], [], "needs_review", True, False),
        # errors-only：出错导致什么都没写 → 与异常隔离同一条释放路径（DEV-072 的核心）
        ({}, [], [], [{"code": "LLM_BUDGET_EXHAUSTED"}], "errors_only", True, True),
        # 真的无事可做：正常终态，不释放
        ({}, [], [], [], "no_change", False, False),
    ],
)
async def test_record_batch_member_truth_table(
    member_error: dict[str, Any],
    mutations: list[dict[str, Any]],
    review_ids: list[dict[str, str]],
    errors: list[dict[str, str]],
    expected: str,
    in_failed: bool,
    released: bool,
) -> None:
    """三个维度的组合各对应什么 outcome、是否进 batch_failed、是否释放（真值表可执行版）。"""
    result = await batch_module.record_batch_member(
        _member_state(
            batch_member_error=member_error,
            commit_result={"mutations": mutations},
            review_candidates=review_ids,
            errors=errors,
        ),
        None,
    )
    entry = result["batch_processed"][0]
    assert entry["outcome"] == expected
    # batch_failed 里的条目是 entry 去掉 mutations 的投影，因此按 operation_id 比较
    failed_ids = [item["operation_id"] for item in result["batch_failed"]]
    assert (entry["operation_id"] in failed_ids) == in_failed, result["batch_failed"]
    assert batch_module.is_releasable_failure(entry) is released
    assert len(result["batch_failed"]) == (1 if in_failed else 0)


class _FakeSession:
    """``_release_failed_members`` 只需要一个带 ``begin()`` 的 session（不碰数据库）。"""

    def begin(self) -> Any:
        class _Begin:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Begin()

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False


class _FakeRuntime:
    class context:
        @staticmethod
        def session_factory() -> _FakeSession:
            return _FakeSession()


@pytest.mark.asyncio
async def test_release_failed_members_covers_errors_only_not_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """释放路径只有一个判定入口：异常隔离 + errors-only 释放，needs_review 不释放。"""
    from backend.memory.persistence import operations as ops_repo

    released_ids: list[str] = []

    async def fake_release(session: Any, *, operation_id: Any) -> Any:
        released_ids.append(str(operation_id))
        return ops_repo.BatchMemberReleaseOutcome(
            status=ops_repo.BATCH_MEMBER_RELEASED, attempt_count=1
        )

    monkeypatch.setattr(ops_repo, "release_batch_member", fake_release)
    ids = {name: uuid4() for name in ("a", "b", "c", "d")}
    summary = await batch_module._release_failed_members(
        _FakeRuntime(),  # 只用到 .context.session_factory()，不碰真实数据库
        [
            _failed_entry(str(ids["a"])),
            {
                "operation_id": str(ids["b"]),
                "outcome": batch_module.MEMBER_OUTCOME_ERRORS_ONLY,
                "reason": batch_module.MEMBER_ERRORS_REASON,
                "error_codes": ["LLM_BUDGET_EXHAUSTED"],
            },
            {
                "operation_id": str(ids["c"]),
                "outcome": batch_module.MEMBER_OUTCOME_NEEDS_REVIEW,
                "reason": "needs_review",
            },
            {"operation_id": str(ids["d"]), "outcome": batch_module.MEMBER_OUTCOME_NO_CHANGE},
        ],
    )
    # 只放行两类"成员自己没写入"的失败；needs_review 与 no_change 都不释放
    assert released_ids == [str(ids["a"]), str(ids["b"])]
    assert summary.released == 2
    assert summary.dead_lettered == 0


@pytest.mark.asyncio
async def test_finalize_mixed_batch_releases_errors_only_member_and_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """混合批次（一条正常 + 一条 errors-only）：批次 succeeded，错误那条被释放且公开可见。"""
    stub = _ReleaseStub()
    monkeypatch.setattr(batch_module, "_release_failed_members", stub)
    errors_only = {
        "operation_id": "err-member",
        "outcome": batch_module.MEMBER_OUTCOME_ERRORS_ONLY,
        "reason": batch_module.MEMBER_ERRORS_REASON,
        "error_codes": ["LLM_BUDGET_EXHAUSTED"],
        "mutations": [],
        "review_candidate_ids": [],
    }
    state = _state(
        batch_processed=[
            errors_only,
            {
                "operation_id": "ok-member",
                "outcome": batch_module.MEMBER_OUTCOME_SUCCEEDED,
                "mutations": [{"mutation_id": "m1"}],
                "review_candidate_ids": [],
            },
        ],
        batch_failed=[errors_only],
        batch_warnings=[],
    )
    result = await batch_module.finalize_batch_result(state, None)

    # 批次本身仍 succeeded（§5.8：单成员失败不拖垮整批），且带上正常成员写入的 mutation
    assert result["commit_result"]["mutations"] == [{"mutation_id": "m1"}]
    warnings = result["warnings"]
    # 计数只算真正没写进去的那一条（旧实现会因为 `or errors` 而虚高）
    assert any("本批 1 条成员未直接写入" in item for item in warnings), warnings
    # 逐条可观测：是哪条成员、为什么（错误码）
    assert any(
        "err-member" in item and "未写入任何变更" in item and "LLM_BUDGET_EXHAUSTED" in item
        for item in warnings
    ), warnings
    # 释放路径收到的是**全部** batch_failed 条目，由 is_releasable_failure 决定放行谁
    assert [entry["operation_id"] for entry in stub.calls[0]] == ["err-member"]


@pytest.mark.asyncio
async def test_finalize_written_member_with_errors_is_not_counted_unwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写入成功但伴随 errors 的成员不算"未直接写入"，但要有"请人工确认完整性"的警告。"""
    stub = _ReleaseStub()
    monkeypatch.setattr(batch_module, "_release_failed_members", stub)
    written = {
        "operation_id": "partial-member",
        "outcome": batch_module.MEMBER_OUTCOME_SUCCEEDED,
        "error_codes": ["LLM_BUDGET_EXHAUSTED"],
        "mutations": [{"mutation_id": "m1"}],
        "review_candidate_ids": [],
    }
    result = await batch_module.finalize_batch_result(
        _state(batch_processed=[written], batch_failed=[], batch_warnings=[]), None
    )
    warnings = result["warnings"]
    assert not any("未直接写入" in item for item in warnings), warnings
    assert any(
        "partial-member" in item and "已写入 1 条变更" in item and "人工确认完整性" in item
        for item in warnings
    ), warnings
    assert stub.calls[0] == []


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
