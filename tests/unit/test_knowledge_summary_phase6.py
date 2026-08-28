"""知识总结 Phase 6 的离线门禁测试。"""

from __future__ import annotations

import importlib
import json
from argparse import Namespace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from backend.conversation.cli.knowledge_summary import _payload_was_scrubbed, _require_apply
from backend.conversation.persistence.knowledge_summary_retention import auto_suspension_reasons
from evals import run_knowledge_summary_eval as eval_runner


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"queue_depth": 5000}, ["QUEUE_DEPTH"]),
        (
            {"oldest_at": datetime(2026, 8, 18, tzinfo=UTC) - timedelta(seconds=601)},
            ["OLDEST_JOB_AGE"],
        ),
        ({"failure_calls": 10, "total_calls": 20}, ["MODEL_FAILURE_RATE"]),
        ({"tokens_today": 1001}, ["DAILY_TOKEN_BUDGET"]),
    ],
)
def test_auto_suspension_reasons_cover_each_frozen_threshold(
    overrides: dict[str, object], expected: list[str]
) -> None:
    """§21.5 的四类阈值均会触发，且边界参数由调用方统一传入。"""
    now = datetime(2026, 8, 18, tzinfo=UTC)
    values: dict[str, object] = {
        "queue_depth": 0,
        "oldest_at": None,
        "failure_calls": 0,
        "total_calls": 0,
        "tokens_today": 0,
        "queue_limit": 5000,
        "oldest_limit_seconds": 600,
        "failure_rate": 0.50,
        "minimum_calls": 20,
        "daily_token_budget": 1000,
        "now": now,
    }
    values.update(overrides)
    assert auto_suspension_reasons(**values) == expected  # type: ignore[arg-type]


def test_auto_suspension_does_not_fire_at_non_breaching_boundaries() -> None:
    """最老 Job 和 token 预算均采用方案指定的严格大于号。"""
    now = datetime(2026, 8, 18, tzinfo=UTC)
    assert (
        auto_suspension_reasons(
            queue_depth=4999,
            oldest_at=now - timedelta(seconds=600),
            failure_calls=9,
            total_calls=20,
            tokens_today=1000,
            queue_limit=5000,
            oldest_limit_seconds=600,
            failure_rate=0.50,
            minimum_calls=20,
            daily_token_budget=1000,
            now=now,
        )
        == []
    )


def test_mutating_cli_requires_operator_and_ticket_id() -> None:
    """所有 apply 操作都必须携带可审计的操作人和变更单。"""
    with pytest.raises(SystemExit, match="--operator"):
        _require_apply(Namespace(apply=True, operator=None, ticket_id=None))
    _require_apply(Namespace(apply=False, operator=None, ticket_id=None))
    _require_apply(Namespace(apply=True, operator="on-call", ticket_id="OPS-2026-08"))


def test_ops_retry_refuses_scrubbed_payload() -> None:
    """retention 后没有完整结构化 payload，不能伪造可重试 Job。"""
    assert _payload_was_scrubbed({"extraction_result": {"scrubbed": True}})
    assert not _payload_was_scrubbed({"input_manifest": {"input_hash": "a" * 64}})


def test_phase6_eval_dataset_has_200_valid_cases(tmp_path: Path) -> None:
    """评测 Runner 默认离线校验数据集，不产生真实模型网络调用。"""
    dataset = Path("evals/knowledge_summary_cases_v1.jsonl")
    output = tmp_path / "report.json"
    exit_code = eval_runner.main(
        [
            "--dataset",
            str(dataset),
            "--model-snapshot",
            "knowledge-summary-test-snapshot",
            "--prompt-version",
            "knowledge_extract_v1",
            "--extract-schema-version",
            "knowledge_extract_schema_v1",
            "--merge-schema-version",
            "knowledge_merge_schema_v1",
            "--normalizer-version",
            "knowledge_canonical_v1",
            "--output",
            str(output),
        ]
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert report["case_count"] == 200
    assert report["network_calls"] == 0
    assert report["annotation_states"] == {"pending_human_double_annotation": 200}
    assert report["human_annotation_ready"] is False
    assert report["structure_errors"] == []
    assert report["status"] == "dataset_pending_human_annotation"
    assert report["gate"]["passed"] is False


def test_model_call_attempt_migration_downgrade_is_explicitly_irreversible() -> None:
    """0005 不得尝试恢复会破坏重试审计历史的旧唯一约束。"""
    migration = importlib.import_module(
        "conversation_migrations.versions.0005_knowledge_summary_model_call_attempts"
    )
    with pytest.raises(RuntimeError, match="不可逆"):
        migration.downgrade()
