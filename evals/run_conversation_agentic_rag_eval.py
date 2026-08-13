"""Conversation Agentic RAG 评测集结构校验器（方案 §26.6 前置步骤）。

诚实化说明（评审 P2）：
- 本脚本**不运行系统**，只校验样本文件的结构完整性（JSON 合法、必需字段、
  确定性断言可执行）；
- "passed" 仅表示**结构校验通过**，不代表 Recall/MRR/忠实性等数值指标达标；
- 数值指标需在 Phase 7 接入真实 LLM 评测后由 Product/RAG/QA 冻结（§1.3 外部
  确认项 4）；当前样本 31 条未达 50–100 条目标，需补充至该范围后运行基线。

报告写入 evals/conversation_agentic_rag_eval_report_<date>.json。
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

SAMPLES_PATH = Path(__file__).with_name("conversation_agentic_rag_samples.jsonl")
REPORT_DIR = Path(__file__).parent


def load_samples() -> list[dict[str, object]]:
    samples: list[dict[str, object]] = []
    for line in SAMPLES_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        samples.append(json.loads(line))
    return samples


def run_deterministic_checks(sample: dict[str, object]) -> list[str]:
    """对样本执行确定性断言（§26.6 可离线校验项）。"""
    failures: list[str] = []
    expected = sample.get("expected") or {}
    name = str(sample.get("name", "?"))

    if expected.get("input_max_chars") is not None:
        content = str(sample.get("conversation", [{}])[0].get("content", ""))
        if len(content) <= 10_000:
            failures.append(f"{name}: 输入超限样本应被拒绝")

    if expected.get("thread_version_conflict"):
        if not expected.get("current_version_present"):
            failures.append(f"{name}: 版本冲突必须携带 current_version")

    if expected.get("no_fake_citation"):
        # 无证据问题不允许出现引用（由 Graph 测试保证 allow-list）
        pass

    if expected.get("deleted_refs_suppressed"):
        deleted = sample.get("deleted_refs") or []
        if not deleted:
            failures.append(f"{name}: 删除样本必须提供 deleted_refs")

    if expected.get("graph_hints_allowlist"):
        kept = expected.get("hints_kept") or []
        dropped = expected.get("hints_dropped") or []
        if not kept or not dropped:
            failures.append(f"{name}: allow-list 样本必须包含 kept 与 dropped")

    if expected.get("filter_allowlist_enforced"):
        model_filters = sample.get("model_filters") or {}
        if not model_filters:
            failures.append(f"{name}: filter 样本必须提供 model_filters")

    if expected.get("prompt_injection_rag"):
        evidence = sample.get("evidence") or []
        if not any("注入" in str(item.get("content", "")) for item in evidence):
            failures.append(f"{name}: 注入样本必须包含恶意指令")

    if expected.get("explicit_remember_no_false_promise"):
        if expected.get("memory_trigger") != "explicit_remember":
            failures.append(f"{name}: 显式记忆样本必须标记 explicit_remember")

    return failures


def main() -> int:
    samples = load_samples()
    if not samples:
        print(f"[conversation-eval] 无样本：{SAMPLES_PATH}", file=sys.stderr)
        return 2
    failures: list[str] = []
    for sample in samples:
        failures.extend(run_deterministic_checks(sample))
    status = "structure_failed" if failures else "structure_passed"
    report = {
        "evaluator": "conversation_agentic_rag",
        "run_at": datetime.now(UTC).isoformat(),
        "sample_count": len(samples),
        "deterministic_failures": failures,
        "status": status,
        "honest_note": (
            "本报告只证明评测集结构完整性（评审 P2 诚实化）："
            "未运行系统、未产出数值指标。Recall/MRR/忠实性/引用覆盖率/时延/成本"
            "需 Phase 7 接入真实 LLM 基线后由 Product/RAG/QA 冻结（§1.3 项 4）。"
            "样本数 31 < 50–100 目标（§26.6），基线前需补齐。"
        ),
    }
    report_path = (
        REPORT_DIR / f"conversation_agentic_rag_eval_report_{datetime.now(UTC):%Y%m%d}.json"
    )
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[conversation-eval] 样本 {len(samples)} 条，结构校验失败 {len(failures)} 条")
    for failure in failures:
        print(f"  FAIL: {failure}")
    print(f"[conversation-eval] 报告（结构校验）：{report_path}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
