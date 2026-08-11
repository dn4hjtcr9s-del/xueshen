"""契约判别联合与跨字段校验单元测试（§5 / §6 / §23.1）。"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter, ValidationError

from backend.memory.contracts.commands import (
    CommitMutationPlan,
    GraphStatePutRequest,
    MaintenanceCommand,
    MemoryPayload,
    ReviewCandidateCommand,
)
from backend.memory.contracts.operations import MemoryOperation

payload_adapter = TypeAdapter(MemoryPayload)


def _now() -> datetime:
    return datetime.now(UTC)


def test_payload_union_accepts_conversation_evidence() -> None:
    payload = payload_adapter.validate_python(
        {
            "kind": "conversation_evidence",
            "thread_id": "t-1",
            "message_ids": ["m-1"],
            "trigger": "turn_boundary",
        }
    )
    assert payload.kind == "conversation_evidence"


def test_payload_union_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        payload_adapter.validate_python({"kind": "drop_table"})


def test_payload_union_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        payload_adapter.validate_python(
            {
                "kind": "conversation_evidence",
                "thread_id": "t-1",
                "message_ids": ["m-1"],
                "trigger": "turn_boundary",
                "user_id": str(uuid.uuid4()),
            }
        )


def test_review_candidate_cross_field_validation() -> None:
    # correct 必须提供 corrected_content
    with pytest.raises(ValidationError):
        ReviewCandidateCommand(candidate_id=uuid.uuid4(), decision="correct")
    # accept 禁止 corrected_content
    with pytest.raises(ValidationError):
        ReviewCandidateCommand(
            candidate_id=uuid.uuid4(),
            decision="accept",
            corrected_content={"replacement_type": "learner"},
        )
    # merge_existing 必须提供 target_memory_id
    with pytest.raises(ValidationError):
        ReviewCandidateCommand(
            candidate_id=uuid.uuid4(),
            decision="accept",
            resolution_target="merge_existing",
        )
    # create_new_topic 禁止 target_memory_id
    with pytest.raises(ValidationError):
        ReviewCandidateCommand(
            candidate_id=uuid.uuid4(),
            decision="accept",
            resolution_target="create_new_topic",
            target_memory_id="mastery:一致收敛",
        )
    ok = ReviewCandidateCommand(
        candidate_id=uuid.uuid4(),
        decision="accept",
        resolution_target="merge_existing",
        target_memory_id="mastery:一致收敛",
    )
    assert ok.resolution_target == "merge_existing"


def test_graph_state_put_request_forbids_kind_and_node_id() -> None:
    with pytest.raises(ValidationError):
        GraphStatePutRequest.model_validate({"action": "mark_familiar", "node_id": "n001"})
    with pytest.raises(ValidationError):
        GraphStatePutRequest.model_validate({"action": "mark_familiar", "kind": "set_graph_state"})


def test_commit_mutation_plan_consistency() -> None:
    base = {
        "mutation_id": uuid.uuid4(),
        "memory_id": "learner",
        "target_memory_type": "learner",
        "action": "create",
    }
    CommitMutationPlan.model_validate(base)
    # mastery patch 不允许出现在 learner 计划
    with pytest.raises(ValidationError):
        CommitMutationPlan.model_validate({**base, "mastery_patch": {}})
    # create 不允许并发令牌
    with pytest.raises(ValidationError):
        CommitMutationPlan.model_validate({**base, "expected_version": 3})
    # merge 必须有 expected_version
    with pytest.raises(ValidationError):
        CommitMutationPlan.model_validate({**base, "action": "merge"})
    # mastery 计划 memory_id 必须 mastery: 前缀
    with pytest.raises(ValidationError):
        CommitMutationPlan.model_validate(
            {
                **base,
                "memory_id": "learner",
                "target_memory_type": "mastery",
            }
        )


def test_maintenance_command_batch_bounds() -> None:
    MaintenanceCommand(kind="purge_tombstones", batch_size=1000)
    with pytest.raises(ValidationError):
        MaintenanceCommand(kind="purge_tombstones", batch_size=1001)


def test_memory_operation_envelope() -> None:
    op = MemoryOperation.model_validate(
        {
            "operation_id": str(uuid.uuid4()),
            "idempotency_key": "k-1",
            "user_id": str(uuid.uuid4()),
            "actor_type": "user",
            "input_kind": "command",
            "operation_type": "forget_memory",
            "priority": 100,
            "occurred_at": _now().isoformat(),
            "payload": {
                "kind": "forget_memory",
                "memory_id": "mastery:一致收敛",
                "expected_version": 2,
            },
            "trace_id": "a" * 32,
            "graph_thread_id": "memory-op:" + str(uuid.uuid4()),
        }
    )
    assert op.payload.kind == "forget_memory"
    assert op.schema_version == 1


def test_projection_command_cross_field() -> None:
    from backend.memory.contracts.commands import ProjectSummaryToGraphCommand

    # changed 必须 apply_active_version + mapping + evidence
    with pytest.raises(ValidationError):
        ProjectSummaryToGraphCommand.model_validate(
            {
                "trigger_event_type": "memory.changed",
                "projection_action": "recompute_without_deleted_version",
                "source_memory_id": "mastery:x",
                "source_version": 1,
                "node_id": "n001",
            }
        )
    # deleted 必须 recompute_without_deleted_version
    ok = ProjectSummaryToGraphCommand.model_validate(
        {
            "trigger_event_type": "memory.deleted",
            "projection_action": "recompute_without_deleted_version",
            "source_memory_id": "mastery:x",
            "source_version": 2,
            "node_id": "n001",
        }
    )
    assert ok.trigger_event_type == "memory.deleted"
