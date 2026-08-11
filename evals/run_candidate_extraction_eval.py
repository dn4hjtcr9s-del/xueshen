"""§9 候选提取评测样例运行器（手动验收，不进 CI）。

用法（DeepSeek/OpenAI 兼容端点）：
  set -a; source .env; set +a
  export OPENAI_API_KEY="$DEEPSEEK_API_KEY" OPENAI_BASE_URL="$DEEPSEEK_BASE_URL" \
         OPENAI_MEMORY_MODEL="$DEEPSEEK_MODEL"
  uv run python -m evals.run_candidate_extraction_eval

每条样例独立调用真实模型，按期望值判定：
- expected.candidates：结果中须存在同 memory_type+category 的候选，且其
  evidence_refs 覆盖期望 refs；long_term_value 不一致记为失败并注明。
- expected.candidates 为空：结果不得产生任何候选。
- ignored_reason_codes_contains / must_not_use_refs / must_not_contain 逐条校验。
报告写入 evals/candidate_extraction_eval_report_<model>.json。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from backend.memory.contracts.common import canonical_json
from backend.memory.graph.openai_client import RealMemoryLLMClient
from backend.memory.graph.policies import LLMCallBudget
from backend.settings import get_settings

SAMPLES = Path(__file__).parent / "candidate_extraction_samples.jsonl"


def _payload(items: list[dict[str, Any]]) -> str:
    return canonical_json(
        {
            "hints": {},
            "items": [
                {
                    "source_ref": item["source_ref"],
                    "role": item["role"],
                    "content": item["content"],
                }
                for item in items
            ],
        }
    )


def _judge(sample: dict[str, Any], result: Any) -> list[str]:
    """返回失败原因列表；空列表表示通过。"""
    failures: list[str] = []
    expected = sample["expected"]
    actual_candidates = [
        {
            "memory_type": c.memory_type,
            "category": c.category,
            "long_term_value": c.long_term_value,
            "refs": [e.evidence_ref for e in c.evidence],
        }
        for c in result.candidates
    ]

    expected_candidates = expected.get("candidates", [])
    # 仅当样例显式声明 "candidates": [] 时才要求无候选；
    # 未声明 candidates 键表示该样例不约束候选数量（如 privacy_filtered）。
    if "candidates" in expected and not expected_candidates and actual_candidates:
        failures.append(f"期望无候选，实际产生 {len(actual_candidates)} 个: {actual_candidates}")
    for exp in expected_candidates:
        matches = [
            c
            for c in actual_candidates
            if c["memory_type"] == exp["memory_type"] and c["category"] == exp["category"]
        ]
        if not matches:
            failures.append(
                f"缺少候选 {exp['memory_type']}/{exp['category']}（实际: {actual_candidates}）"
            )
            continue
        ref_cover = any(set(exp["evidence_refs"]) <= set(c["refs"]) for c in matches)
        if not ref_cover:
            failures.append(
                f"{exp['memory_type']}/{exp['category']} 证据 refs 未覆盖 "
                f"{exp['evidence_refs']}（实际: {[c['refs'] for c in matches]}）"
            )
        value_match = any(c["long_term_value"] == exp["long_term_value"] for c in matches)
        if not value_match:
            failures.append(
                f"{exp['memory_type']}/{exp['category']} long_term_value 期望 "
                f"{exp['long_term_value']}（实际: {[c['long_term_value'] for c in matches]}）"
            )

    for code in expected.get("ignored_reason_codes_contains", []):
        if code not in result.ignored_reason_codes:
            failures.append(
                f"ignored_reason_codes 缺少 {code}（实际: {result.ignored_reason_codes}）"
            )
    used_refs = {ref for c in actual_candidates for ref in c["refs"]}
    for ref in expected.get("must_not_use_refs", []):
        if ref in used_refs:
            failures.append(f"使用了禁止的证据 ref: {ref}")
    dumped = json.dumps(result.model_dump(mode="json"), ensure_ascii=False)
    for text in expected.get("must_not_contain", []):
        if text in dumped:
            failures.append(f"输出包含禁止内容: {text}")
    return failures


async def _run() -> int:
    settings = get_settings()
    if not settings.openai_api_key:
        print("[eval] 未配置 OPENAI_API_KEY", file=sys.stderr)
        return 2
    client = RealMemoryLLMClient(settings=settings)
    samples = [
        json.loads(line)
        for line in SAMPLES.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    report: dict[str, Any] = {
        "model": settings.openai_memory_model,
        "base_url": settings.openai_base_url,
        "run_at": datetime.now(UTC).isoformat(),
        "samples": [],
    }
    passed = 0
    for sample in samples:
        name = sample["name"]
        try:
            result, _record = await client.extract_candidates(
                source_payload=_payload(sample["source_items"]), budget=LLMCallBudget()
            )
            failures = _judge(sample, result)
            actual = result.model_dump(mode="json")
        except Exception as exc:  # 模型/协议层失败也算样例失败
            failures = [f"调用异常: {type(exc).__name__}: {str(exc)[:300]}"]
            actual = None
        ok = not failures
        passed += int(ok)
        report["samples"].append(
            {
                "name": name,
                "description": sample["description"],
                "pass": ok,
                "failures": failures,
                "actual": actual,
            }
        )
        print(f"[eval] {'PASS' if ok else 'FAIL'} {name}: {sample['description']}")
        for failure in failures:
            print(f"       - {failure}")

    total = len(samples)
    report["summary"] = {"total": total, "passed": passed, "pass_rate": passed / total}
    out = SAMPLES.parent / f"candidate_extraction_eval_report_{settings.openai_memory_model}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[eval] 通过 {passed}/{total}，报告: {out}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
