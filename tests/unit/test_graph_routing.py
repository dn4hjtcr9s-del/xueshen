"""父图路由与授权单元测试（§10.3 / §18.2）。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from backend.memory.contracts.commands import (
    ForgetMemoryCommand,
    GraphStateCommand,
    MaintenanceCommand,
)
from backend.memory.contracts.errors import InvalidPayloadError
from backend.memory.contracts.evidence import ActivityEvidence, ConversationEvidence
from backend.memory.graph import manager
from backend.memory.graph.state import MemoryManagerState

USER = UUID("00000000-0000-4000-8000-000000000020")


def _op_state(
    payload, operation_type: str, actor_type: str = "user", input_kind: str = "evidence"
) -> MemoryManagerState:
    return {
        "operation": {
            "operation_id": str(uuid4()),
            "idempotency_key": "k1",
            "user_id": str(USER),
            "actor_type": actor_type,
            "input_kind": input_kind,
            "operation_type": operation_type,
            "priority": 50,
            "occurred_at": datetime.now(UTC).isoformat(),
            "payload": payload.model_dump(mode="json"),
            "trace_id": uuid4().hex + uuid4().hex,
            "graph_thread_id": "g1",
        }
    }


@pytest.mark.parametrize(
    ("activity_type", "expected"),
    [
        ("page_view", "activity_exposure"),
        ("bookmark", "activity_exposure"),
        ("check_in", "activity_exposure"),
        ("exercise_attempt", "summary"),
        ("forum_post", "summary"),
        ("review_result", "summary"),
    ],
)
async def test_activity_routing_split(activity_type: str, expected: str) -> None:
    payload = ActivityEvidence(activity_type=activity_type, activity_ids=["a1"])
    state = _op_state(payload, "activity_evidence", actor_type="activity_agent")
    result = await manager.route_operation(state, None)  # type: ignore[arg-type]
    assert result["route"] == expected


async def test_command_and_maintenance_routing() -> None:
    forget = ForgetMemoryCommand(memory_id="mastery:t1", expected_version=1)
    state = _op_state(forget, "forget_memory")
    assert (await manager.route_operation(state, None))["route"] == "memory_command"  # type: ignore[arg-type]

    graph_cmd = GraphStateCommand(node_id="n001", action="mark_familiar")
    state = _op_state(graph_cmd, "set_graph_state")
    assert (await manager.route_operation(state, None))["route"] == "graph_state"  # type: ignore[arg-type]

    maint = MaintenanceCommand(kind="rebuild_index")
    state = _op_state(maint, "rebuild_index", actor_type="system")
    assert (await manager.route_operation(state, None))["route"] == "maintenance"  # type: ignore[arg-type]


async def test_authorize_actor_matrix() -> None:
    conv = ConversationEvidence(thread_id="t1", message_ids=["m1"], trigger="turn_boundary")
    # conversation_agent 可以提交证据
    state = _op_state(conv, "conversation_evidence", actor_type="conversation_agent")
    assert await manager.authorize_actor(state, None) == {}  # type: ignore[arg-type]
    # user 不能走 evidence input_kind
    state = _op_state(conv, "conversation_evidence", actor_type="user")
    with pytest.raises(InvalidPayloadError, match="不允许"):
        await manager.authorize_actor(state, None)  # type: ignore[arg-type]
    # conversation_agent 不能执行命令
    forget = ForgetMemoryCommand(memory_id="mastery:t1", expected_version=1)
    state = _op_state(
        forget, "forget_memory", actor_type="conversation_agent", input_kind="command"
    )
    with pytest.raises(InvalidPayloadError, match="不允许"):
        await manager.authorize_actor(state, None)  # type: ignore[arg-type]


async def test_normalize_input_rejects_kind_mismatch() -> None:
    conv = ConversationEvidence(thread_id="t1", message_ids=["m1"], trigger="turn_boundary")
    state = _op_state(conv, "forget_memory", actor_type="conversation_agent")
    with pytest.raises(InvalidPayloadError, match="不一致"):
        await manager.normalize_input(state, None)  # type: ignore[arg-type]


async def test_normalize_result_status_mapping() -> None:
    base = _op_state(
        ConversationEvidence(thread_id="t1", message_ids=["m1"], trigger="turn_boundary"),
        "conversation_evidence",
        actor_type="conversation_agent",
    )

    state = MemoryManagerState(
        **base,  # type: ignore[arg-type]
        commit_result={
            "mutations": [
                {
                    "mutation_id": str(uuid4()),
                    "memory_id": "learner",
                    "action": "create",
                    "before_version": None,
                    "after_version": 1,
                }
            ]
        },
        warnings=[],
    )
    result = await manager.normalize_result(state, None)  # type: ignore[arg-type]
    assert result["commit_result"]["status"] == "succeeded"

    state = MemoryManagerState(
        **base,  # type: ignore[arg-type]
        commit_result={"mutations": [], "review_candidate_ids": [str(uuid4())]},
        warnings=[],
    )
    result = await manager.normalize_result(state, None)  # type: ignore[arg-type]
    assert result["commit_result"]["status"] == "needs_review"

    state = MemoryManagerState(
        **base,  # type: ignore[arg-type]
        commit_result={"mutations": []},
        errors=[{"code": "LLM_BUDGET_EXHAUSTED"}],
        warnings=[],
    )
    result = await manager.normalize_result(state, None)  # type: ignore[arg-type]
    assert result["commit_result"]["status"] == "dead_letter"
