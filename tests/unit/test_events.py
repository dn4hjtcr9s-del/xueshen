"""领域事件类型绑定测试（§15）。"""

import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from backend.memory.contracts.events import (
    EVENT_PAYLOAD_TYPES,
    MemoryChangedPayload,
    TypedMemoryDomainEvent,
)


def _envelope(event_type: str, payload: dict) -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "user_id": str(uuid.uuid4()),
        "aggregate_type": "memory",
        "aggregate_id": "mastery:一致收敛",
        "aggregate_version": 4,
        "occurred_at": datetime.now(UTC).isoformat(),
        "trace_id": "a" * 32,
        "payload": payload,
    }


def test_all_event_types_have_payload_model() -> None:
    expected = {
        "memory.changed",
        "memory.deleted",
        "memory.restored",
        "learner.updated",
        "review_candidate.created",
        "review_candidate.resolved",
        "graph_state.changed",
        "graph_state.explanation_available",
        "account_memory.purge_requested",
    }
    assert set(EVENT_PAYLOAD_TYPES) == expected


def test_memory_changed_valid() -> None:
    event = TypedMemoryDomainEvent.model_validate(
        _envelope(
            "memory.changed",
            {
                "schema_version": 1,
                "memory_id": "mastery:一致收敛",
                "memory_type": "mastery",
                "before_version": 3,
                "after_version": 4,
                "topic_key": "一致收敛",
                "graph_projection_candidates": ["n067"],
            },
        )
    )
    typed = event.typed_payload()
    assert isinstance(typed, MemoryChangedPayload)
    assert typed.after_version == 4


def test_unknown_event_type_rejected() -> None:
    with pytest.raises(ValidationError):
        TypedMemoryDomainEvent.model_validate(_envelope("memory.hacked", {}))


def test_payload_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        TypedMemoryDomainEvent.model_validate(
            _envelope(
                "memory.changed",
                {
                    "schema_version": 1,
                    "memory_id": "mastery:x",
                    "memory_type": "mastery",
                    "after_version": 1,
                    "topic_key": "x",
                    "raw_markdown": "泄漏",
                },
            )
        )
