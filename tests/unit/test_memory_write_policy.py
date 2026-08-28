"""总结记忆写入的确定性证据门禁测试。"""

from __future__ import annotations

import pytest

from backend.memory.graph.policies import (
    candidate_evidence_issues,
    evidence_gate_disposition,
    validate_commit_evidence_refs,
)


def _candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "memory_type": "mastery",
        "topic_title": "导数",
        "category": "understanding",
        "summary": "用户能正确计算导数",
        "evidence": [
            {
                "evidence_ref": "message:user:1",
                "evidence_type": "user_solution",
                "summary": "用户独立完成计算",
                "strength": 0.9,
            }
        ],
    }
    candidate.update(overrides)
    return candidate


def test_valid_user_evidence_can_keep_auto_save() -> None:
    candidate = _candidate()
    sources = [{"source_ref": "message:user:1", "role": "user"}]

    assert candidate_evidence_issues(candidate, sources) == []
    assert (
        evidence_gate_disposition(
            candidate=candidate,
            source_items=sources,
            disposition="auto_save",
        )
        == "auto_save"
    )


def test_unknown_evidence_ref_is_discarded() -> None:
    candidate = _candidate()

    issues = candidate_evidence_issues(candidate, [])

    assert "MEMORY_EVIDENCE_REF_NOT_FOUND" in issues
    assert "MEMORY_EVIDENCE_NOT_AUDITABLE" in issues
    assert (
        evidence_gate_disposition(
            candidate=candidate,
            source_items=[],
            disposition="auto_save",
        )
        == "discard"
    )


def test_assistant_statement_cannot_auto_save_mastery() -> None:
    candidate = _candidate()
    sources = [{"source_ref": "message:user:1", "role": "assistant"}]

    issues = candidate_evidence_issues(candidate, sources)

    assert "MEMORY_USER_EVIDENCE_REQUIRED" in issues
    assert "MASTERY_MEMORY_REQUIRES_LEARNING_EVIDENCE" in issues
    assert (
        evidence_gate_disposition(
            candidate=candidate,
            source_items=sources,
            disposition="auto_save",
        )
        == "review"
    )


def test_candidate_type_and_category_mismatch_is_not_auto_saved() -> None:
    candidate = _candidate(memory_type="learner", category="understanding", topic_title=None)
    sources = [{"source_ref": "message:user:1", "role": "user"}]

    assert "LEARNER_CATEGORY_MISMATCH" in candidate_evidence_issues(candidate, sources)
    assert (
        evidence_gate_disposition(
            candidate=candidate,
            source_items=sources,
            disposition="auto_save",
        )
        == "review"
    )


def test_mastery_self_report_requires_review() -> None:
    candidate = _candidate(
        evidence=[
            {
                "evidence_ref": "message:user:1",
                "evidence_type": "explicit_user_statement",
                "summary": "用户说自己已经理解",
                "strength": 0.8,
            }
        ]
    )
    sources = [{"source_ref": "message:user:1", "role": "user"}]

    assert "MASTERY_SELF_REPORT_REQUIRES_REVIEW" in candidate_evidence_issues(candidate, sources)
    assert (
        evidence_gate_disposition(
            candidate=candidate,
            source_items=sources,
            disposition="auto_save",
        )
        == "review"
    )


def test_ambiguous_source_ref_is_not_auditable() -> None:
    candidate = _candidate()
    sources = [
        {"source_ref": "message:user:1", "role": "user"},
        {"source_ref": "message:user:1", "role": "assistant"},
    ]

    issues = candidate_evidence_issues(candidate, sources)

    assert "MEMORY_EVIDENCE_REF_AMBIGUOUS" in issues
    assert "MEMORY_EVIDENCE_NOT_AUDITABLE" in issues
    assert (
        evidence_gate_disposition(
            candidate=candidate,
            source_items=sources,
            disposition="auto_save",
        )
        == "discard"
    )


def test_commit_evidence_refs_must_be_from_current_source_bundle() -> None:
    validate_commit_evidence_refs(
        evidence_refs=["message:user:1"],
        allowed_refs={"message:user:1"},
    )

    with pytest.raises(ValueError, match="未授权证据引用"):
        validate_commit_evidence_refs(
            evidence_refs=["message:invented"],
            allowed_refs={"message:user:1"},
        )
