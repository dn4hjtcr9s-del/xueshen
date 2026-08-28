"""知识总结质量评测 Runner（方案 §22.6）。

默认只做离线数据集结构校验，不调用网络或真实模型。提供预测 JSONL 后，Runner 会按冻结
分母计算候选、来源支持、合并、非数学误保存、重复一致性和端到端 Job 指标。

固定用法：
    .venv/bin/python -m evals.run_knowledge_summary_eval \
      --dataset evals/knowledge_summary_cases_v1.jsonl \
      --model-snapshot <explicit-model-snapshot> \
      --prompt-version knowledge_extract_v1 \
      --extract-schema-version knowledge_extract_schema_v1 \
      --merge-schema-version knowledge_merge_schema_v1 \
      --normalizer-version knowledge_canonical_v1
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

_ALLOWED_ACTIONS = {"create", "merge", "no_change", "needs_review"}
_ALLOWED_ROLES = {"user", "assistant"}
_ALLOWED_SPLITS = {"dev", "validation", "test"}
_THRESHOLD = {
    "candidate_precision": (">=", 0.90),
    "unsupported_item_rate": ("<=", 0.01),
    "merge_precision": (">=", 0.95),
    "protected_section_incidents": ("=", 0.0),
    "non_math_false_save_rate": ("<=", 0.01),
    "repeat_consistency": ("=", 1.0),
    "end_to_end_success_or_no_change_rate": (">=", 0.98),
}


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL，并把行号加入错误上下文。"""
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: JSON 无法解析：{exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number}: 每行必须是 JSON object")
        rows.append(value)
    return rows


def _uuid(value: Any, context: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{context}: 不是合法 UUID") from exc


def _validate_dataset(rows: list[dict[str, Any]]) -> list[str]:
    """校验 §22.6 固定字段和标注结构，返回可读错误列表。"""
    errors: list[str] = []
    seen_ids: set[str] = set()
    for index, case in enumerate(rows, 1):
        prefix = f"case[{index}]"
        for key in ("case_id", "split", "tags", "messages", "existing_summaries", "gold"):
            if key not in case:
                errors.append(f"{prefix}: 缺少字段 {key}")
        case_id = str(case.get("case_id", ""))
        if not case_id:
            errors.append(f"{prefix}: case_id 不能为空")
        elif case_id in seen_ids:
            errors.append(f"{prefix}: case_id 重复 {case_id}")
        seen_ids.add(case_id)
        if case.get("split") not in _ALLOWED_SPLITS:
            errors.append(f"{prefix}: split 必须是 {_ALLOWED_SPLITS}")
        if not isinstance(case.get("tags"), list) or not case["tags"]:
            errors.append(f"{prefix}: tags 必须是非空数组")
        messages = case.get("messages")
        message_ids: set[str] = set()
        if not isinstance(messages, list) or not messages:
            errors.append(f"{prefix}: messages 必须是非空数组")
            messages = []
        sequences: list[int] = []
        for message_index, message in enumerate(messages, 1):
            message_prefix = f"{prefix}.messages[{message_index}]"
            if not isinstance(message, dict):
                errors.append(f"{message_prefix}: 必须是 object")
                continue
            for key in ("message_id", "role", "sequence", "content"):
                if key not in message:
                    errors.append(f"{message_prefix}: 缺少字段 {key}")
            if "message_id" in message:
                try:
                    message_id = _uuid(message["message_id"], message_prefix)
                    if message_id in message_ids:
                        errors.append(f"{message_prefix}: message_id 重复")
                    message_ids.add(message_id)
                except ValueError as exc:
                    errors.append(str(exc))
            if message.get("role") not in _ALLOWED_ROLES:
                errors.append(f"{message_prefix}: role 非法")
            if not isinstance(message.get("sequence"), int) or message.get("sequence", 0) < 1:
                errors.append(f"{message_prefix}: sequence 必须是正整数")
            else:
                sequences.append(message["sequence"])
            if not isinstance(message.get("content"), str) or not message["content"].strip():
                errors.append(f"{message_prefix}: content 不能为空")
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            errors.append(f"{prefix}: messages.sequence 必须严格递增且唯一")

        summaries = case.get("existing_summaries")
        if not isinstance(summaries, list):
            errors.append(f"{prefix}: existing_summaries 必须是数组")
            summaries = []
        for summary_index, summary in enumerate(summaries, 1):
            summary_prefix = f"{prefix}.existing_summaries[{summary_index}]"
            if not isinstance(summary, dict):
                errors.append(f"{summary_prefix}: 必须是 object")
                continue
            for key in ("summary_id", "version", "state_hash", "content"):
                if key not in summary:
                    errors.append(f"{summary_prefix}: 缺少字段 {key}")
            if "summary_id" in summary:
                try:
                    _uuid(summary["summary_id"], summary_prefix)
                except ValueError as exc:
                    errors.append(str(exc))
            if not isinstance(summary.get("version"), int) or summary.get("version", 0) < 1:
                errors.append(f"{summary_prefix}: version 必须是正整数")
            if not isinstance(summary.get("state_hash"), str) or len(summary["state_hash"]) != 64:
                errors.append(f"{summary_prefix}: state_hash 必须是 64 字符 hash")
            if not isinstance(summary.get("content"), dict):
                errors.append(f"{summary_prefix}: content 必须是 object")

        gold = case.get("gold")
        if not isinstance(gold, dict):
            errors.append(f"{prefix}: gold 必须是 object")
            continue
        if not isinstance(gold.get("should_generate"), bool):
            errors.append(f"{prefix}.gold: should_generate 必须是 bool")
        expected_action = gold.get("expected_action")
        if expected_action not in _ALLOWED_ACTIONS:
            errors.append(f"{prefix}.gold: expected_action 非法")
        candidates = gold.get("candidates")
        if not isinstance(candidates, list):
            errors.append(f"{prefix}.gold: candidates 必须是数组")
            candidates = []
        for candidate_index, candidate in enumerate(candidates):
            candidate_prefix = f"{prefix}.gold.candidates[{candidate_index}]"
            if not isinstance(candidate, dict):
                errors.append(f"{candidate_prefix}: 必须是 object")
                continue
            for key in (
                "target_summary_key",
                "topic_group_title",
                "topic_title",
                "sections",
                "source_message_ids",
                "action",
            ):
                if key not in candidate:
                    errors.append(f"{candidate_prefix}: 缺少字段 {key}")
            if candidate.get("action") not in _ALLOWED_ACTIONS:
                errors.append(f"{candidate_prefix}: action 非法")
            if not isinstance(candidate.get("sections"), dict):
                errors.append(f"{candidate_prefix}: sections 必须是 object")
            if not isinstance(candidate.get("source_message_ids"), list):
                errors.append(f"{candidate_prefix}: source_message_ids 必须是数组")
            else:
                for source_id in candidate["source_message_ids"]:
                    try:
                        normalized = _uuid(source_id, candidate_prefix)
                        if normalized not in message_ids:
                            errors.append(f"{candidate_prefix}: source_message_id 不在 messages 中")
                    except ValueError as exc:
                        errors.append(str(exc))
        if gold.get("target_summary_key") is not None and not isinstance(
            gold.get("target_summary_key"), str
        ):
            errors.append(f"{prefix}.gold: target_summary_key 必须是 string 或 null")
        annotations = case.get("annotations")
        if annotations is not None:
            if not isinstance(annotations, list) or len(annotations) != 2:
                errors.append(f"{prefix}: annotations 若提供必须正好有两名标注者")
            else:
                annotator_ids = [
                    item.get("annotator_id") for item in annotations if isinstance(item, dict)
                ]
                if len(annotator_ids) != 2 or len(set(annotator_ids)) != 2:
                    errors.append(f"{prefix}: 两名标注者必须使用不同 annotator_id")
    return errors


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _section_overlap(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for section, expected_values in expected.items():
        if not isinstance(expected_values, list):
            continue
        actual_values = actual.get(section, [])
        if not isinstance(actual_values, list):
            continue
        if any(str(value) in {str(item) for item in actual_values} for value in expected_values):
            return True
    return not expected


def _candidate_match(
    actual: dict[str, Any], expected: dict[str, Any], message_ids: set[str]
) -> bool:
    if actual.get("target_summary_key") != expected.get("target_summary_key"):
        return False
    if not _section_overlap(actual.get("sections", {}), expected.get("sections", {})):
        return False
    actual_sources = {
        _uuid(value, "prediction.source_message_ids")
        for value in actual.get("source_message_ids", [])
    }
    expected_sources = {
        _uuid(value, "gold.source_message_ids") for value in expected.get("source_message_ids", [])
    }
    return bool(actual_sources & expected_sources) and actual_sources <= message_ids


def _prediction_map(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """读取并校验预测行的唯一 case_id。"""
    result: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows, 1):
        case_id = str(row.get("case_id", ""))
        if not case_id or case_id in result:
            raise ValueError(f"prediction[{index}]: case_id 缺失或重复")
        if not isinstance(row.get("candidates", []), list):
            raise ValueError(f"prediction[{index}]: candidates 必须是数组")
        result[case_id] = row
    return result


def _calculate_metrics(
    cases: list[dict[str, Any]],
    predictions: dict[str, dict[str, Any]],
    repeats: dict[str, dict[str, Any]] | None,
) -> dict[str, float | None]:
    """按方案冻结分母计算离线质量指标。"""
    candidate_total = candidate_correct = 0
    unsupported_total = unsupported_count = 0
    merge_total = merge_correct = 0
    non_math_total = non_math_saved = 0
    repeat_total = repeat_same = 0
    e2e_total = e2e_success = 0
    protected_incidents = 0
    for case in cases:
        prediction = predictions.get(case["case_id"])
        if prediction is None:
            continue
        messages = {
            _uuid(message["message_id"], "dataset.message_id") for message in case["messages"]
        }
        gold_candidates = case["gold"]["candidates"]
        actual_candidates = prediction.get("candidates", [])
        for actual in actual_candidates:
            candidate_total += 1
            if any(_candidate_match(actual, expected, messages) for expected in gold_candidates):
                candidate_correct += 1
            if actual.get("action") == "merge":
                merge_total += 1
                if any(
                    expected.get("action") == "merge"
                    and _candidate_match(actual, expected, messages)
                    for expected in gold_candidates
                ) and not actual.get("protected_section_incident", False):
                    merge_correct += 1
        for item in prediction.get("saved_items", []):
            unsupported_total += 1
            source_ids = {
                _uuid(value, "prediction.saved_items.source_message_ids")
                for value in item.get("source_message_ids", [])
            }
            gold_sources = {
                _uuid(value, "gold.source_message_ids")
                for candidate in gold_candidates
                for value in candidate.get("source_message_ids", [])
            }
            if (
                source_ids <= messages
                and source_ids & gold_sources
                and not item.get("unsupported", False)
            ):
                unsupported_count += 1
        if "non_math" in case.get("tags", []):
            non_math_total += 1
            saved = prediction.get("saved_active_summary")
            if saved is None:
                saved = any(item.get("action") in {"create", "merge"} for item in actual_candidates)
            if saved:
                non_math_saved += 1
        protected_incidents += int(prediction.get("protected_section_incidents", 0) or 0)
        if prediction.get("job_status") is not None:
            e2e_total += 1
            if prediction["job_status"] in {"succeeded", "no_change"}:
                e2e_success += 1
        if repeats is not None and case["case_id"] in repeats:
            repeat_total += 1
            if _canonical(prediction) == _canonical(repeats[case["case_id"]]):
                repeat_same += 1
    return {
        "candidate_precision": candidate_correct / candidate_total if candidate_total else None,
        "unsupported_item_rate": (
            (unsupported_total - unsupported_count) / unsupported_total
            if unsupported_total
            else None
        ),
        "merge_precision": merge_correct / merge_total if merge_total else None,
        "protected_section_incidents": float(protected_incidents),
        "non_math_false_save_rate": (non_math_saved / non_math_total if non_math_total else None),
        "repeat_consistency": repeat_same / repeat_total if repeat_total else None,
        "end_to_end_success_or_no_change_rate": (e2e_success / e2e_total if e2e_total else None),
    }


def _gate(metrics: dict[str, float | None], *, human_annotation_ready: bool) -> dict[str, Any]:
    """只有双人标注完成、所有指标都有值且达到门槛时才允许灰度。"""
    checks: dict[str, bool] = {"human_annotation_ready": human_annotation_ready}
    for name, (operator, threshold) in _THRESHOLD.items():
        value = metrics.get(name)
        if value is None:
            checks[name] = False
        elif operator == ">=":
            checks[name] = value >= threshold
        elif operator == "<=":
            checks[name] = value <= threshold
        else:
            checks[name] = value == threshold
    return {
        "passed": bool(checks) and all(checks.values()),
        "checks": checks,
        "thresholds": _THRESHOLD,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="离线知识总结质量评测")
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--repeat-predictions", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model-snapshot", required=True)
    parser.add_argument("--prompt-version", required=True)
    parser.add_argument("--extract-schema-version", required=True)
    parser.add_argument("--merge-schema-version", required=True)
    parser.add_argument("--normalizer-version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行数据集校验和可选预测评测；默认不访问网络。"""
    args = _build_parser().parse_args(argv)
    try:
        cases = _load_jsonl(args.dataset)
        errors = _validate_dataset(cases)
        if len(cases) < 200:
            errors.append(f"数据集至少需要 200 条 case，实际 {len(cases)} 条")
        predictions = _prediction_map(_load_jsonl(args.predictions)) if args.predictions else {}
        repeats = (
            _prediction_map(_load_jsonl(args.repeat_predictions))
            if args.repeat_predictions
            else None
        )
        unknown_predictions = sorted(set(predictions) - {case["case_id"] for case in cases})
        if unknown_predictions:
            errors.append(f"预测包含未知 case_id：{unknown_predictions[:5]}")
        metrics = (
            _calculate_metrics(cases, predictions, repeats)
            if args.predictions
            else {name: None for name in _THRESHOLD}
        )
        annotation_states = {
            state: sum(case.get("annotation_state") == state for case in cases)
            for state in sorted({str(case.get("annotation_state", "missing")) for case in cases})
        }
        human_annotation_ready = bool(cases) and all(
            case.get("annotation_state") == "adjudicated" for case in cases
        )
        report = {
            "evaluator": "knowledge_summary",
            "run_at": datetime.now(UTC).isoformat(),
            "dataset": str(args.dataset),
            "case_count": len(cases),
            "split_counts": {
                split: sum(case.get("split") == split for case in cases)
                for split in sorted(_ALLOWED_SPLITS)
            },
            "model_snapshot": args.model_snapshot,
            "prompt_version": args.prompt_version,
            "extract_schema_version": args.extract_schema_version,
            "merge_schema_version": args.merge_schema_version,
            "normalizer_version": args.normalizer_version,
            "network_calls": 0,
            "annotation_states": annotation_states,
            "human_annotation_ready": human_annotation_ready,
            "structure_errors": errors,
            "metrics": metrics,
            "gate": (
                _gate(metrics, human_annotation_ready=human_annotation_ready)
                if not errors
                else {"passed": False, "checks": {}}
            )
            if args.predictions
            else {"passed": False, "checks": {}, "reason": "未提供 predictions"},
            "status": (
                "failed"
                if errors
                else "dataset_validated"
                if human_annotation_ready
                else "dataset_pending_human_annotation"
            ),
        }
    except (OSError, ValueError) as exc:
        print(f"[knowledge-summary-eval] {exc}", file=sys.stderr)
        return 2
    output = args.output or args.dataset.with_name(
        f"knowledge_summary_eval_report_{datetime.now(UTC):%Y%m%dT%H%M%SZ}.json"
    )
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"[knowledge-summary-eval] 报告：{output}")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
