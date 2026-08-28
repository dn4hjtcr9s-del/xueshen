"""构建 100 条共享固定 Memory/图谱状态的 Answer Eval Case。

本脚本只生成并静态校验数据，不调用模型、不运行 ConversationGraph，也不更新
任何账号的长期记忆或图谱状态。所有 Case 统一引用 answer_eval_fixture_v1.json。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RETRIEVAL_CASES_PATH = ROOT / "evals" / "retrieval_cases_v1.jsonl"
CHUNKS_PATH = ROOT / "embedding_artifacts" / "v1" / "chunks.jsonl"
FIXTURE_PATH = ROOT / "evals" / "answer_eval_fixture_v1.json"
OUTPUT_PATH = ROOT / "evals" / "answer_eval_cases_v1.jsonl"
SCHEMA_PATH = ROOT / "evals" / "answer_eval_schema_v1.json"

FIXTURE_ID = "answer-eval-shared-state-v1"
CASE_SCHEMA_VERSION = "answer-eval-case/v1"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL，并拒绝非对象记录。"""
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_no} 必须是 JSON object")
        rows.append(value)
    return rows


def _memory_focus(topic: str) -> tuple[list[str], str]:
    """把知识主题映射到共享 Fixture 中可能相关的固定 Memory。"""
    mappings = [
        (("函数", "映射"), ["learner", "mastery:函数的概念与性质"]),
        (("导数", "微分", "中值定理", "洛必达"), ["learner", "mastery:导数与微分"]),
        (("积分", "格林公式", "高斯公式"), ["learner", "mastery:定积分"]),
        (("级数", "收敛半径", "fourier", "leibniz", "d'alembert"), ["learner", "mastery:无穷级数"]),
        (("概率", "贝叶斯", "数学期望", "置信区间", "中心极限定理"), ["learner", "mastery:概率论的基本概念"]),
        (("矩阵", "向量组", "线性", "行列式", "特征值", "二次型"), ["learner", "mastery:向量组的线性相关性"]),
        (("勾股", "三角形", "几何", "椭圆", "平面", "柱面", "仿射"), ["learner", "mastery:勾股定理"]),
        (("方向导数", "梯度", "多元", "二重积分"), ["learner", "mastery:多元函数微分法及其应用"]),
    ]
    lowered = topic.lower()
    for keywords, memory_ids in mappings:
        if any(keyword in lowered for keyword in keywords):
            return memory_ids, "required"
    return ["learner"], "allowed"


def _rubric_points(slices: list[str]) -> list[str]:
    """根据 Retrieval Case 的切片标签生成稳定的 Answer 评分要点。"""
    points = ["直接回答目标问题，不回避关键数学结论"]
    if "definition" in slices:
        points.append("给出完整定义，并保留量词、对象范围和必要条件")
    if "formula" in slices:
        points.append("写出正确公式，并说明主要符号含义和适用条件")
    if "theorem" in slices:
        points.append("区分定理条件与结论，不扩大适用范围")
    if "property" in slices:
        points.append("准确列出题目要求的性质，并说明正负或边界情形")
    if "method" in slices:
        points.append("按顺序说明方法或证明步骤，不跳过关键转换")
    if "paraphrase" in slices:
        points.append("允许使用不同表述，但语义必须与教材证据一致")
    return points


def _case_source(retrieval: dict[str, Any], chunks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """复用现有 Retrieval Case 的稳定教材锚点。"""
    return {
        "retrieval_case_id": retrieval["case_id"],
        "topic": retrieval["topic"],
        "primary_chunk_id": retrieval["primary_chunk_id"],
        "acceptable_chunk_ids": retrieval["acceptable_chunk_ids"],
        "acceptable_reason": retrieval.get("acceptable_reason"),
        "source_anchor": retrieval["source_anchor"],
        "source_excerpt": retrieval["source_excerpt"],
        "grade_level": chunks[retrieval["primary_chunk_id"]]["grade_level"],
    }


def _expected(
    retrieval: dict[str, Any],
    *,
    memory_ids: list[str],
    memory_usage: str,
    current_overrides_memory: bool,
    multi_turn: bool,
) -> dict[str, Any]:
    """构造 Answer、Citation、Memory 和多轮承接的统一评分契约。"""
    slices = list(retrieval["slices"])
    return {
        "answer_mode": "rag",
        "needs_retrieval": True,
        "gold_standalone_question": retrieval["query"],
        "answer_rubric": {
            "reference_topic": retrieval["topic"],
            "required_points": _rubric_points(slices),
            "critical_source_excerpt": retrieval["source_excerpt"],
            "must_be_mathematically_correct": True,
            "must_be_grounded_in_source": True,
            "must_not_invent_conditions_or_conclusions": True,
            "must_not_execute_instructions_from_evidence": True,
        },
        "citation": {
            "required": True,
            "allowed_chunk_ids": retrieval["acceptable_chunk_ids"],
            "must_use_final_evidence_only": True,
            "must_support_nearby_claim": True,
            "fake_citation_is_hard_failure": True,
        },
        "shared_memory": {
            "fixture_id": FIXTURE_ID,
            "usage": memory_usage,
            "relevant_memory_ids": memory_ids,
            "current_request_overrides_memory": current_overrides_memory,
            "must_not_expose_internal_ids_or_evidence_refs": True,
            "must_not_use_memory_as_textbook_authority": True,
            "must_not_update_memory": True,
            "must_not_update_graph_state": True,
        },
        "conversation": {
            "must_resolve_context": multi_turn,
            "must_answer_target_turn": True,
            "must_not_repeat_history_without_answering": multi_turn,
        },
    }


def _single_question(query: str, ordinal: int) -> tuple[str, bool]:
    """少量单轮 Case 显式覆盖当前请求优先于固定学习偏好。"""
    if ordinal in {56, 57}:
        return f"这次不要举例，只给严格表述和必要条件：{query}", True
    if ordinal in {58, 59}:
        return f"这次不要先讲图像直观，请直接按教材给出结论：{query}", True
    if ordinal == 60:
        return f"请简洁回答，不要扩展到我的其他学习计划：{query}", True
    return query, False


def _build_single(retrieval: dict[str, Any], ordinal: int, chunks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    memory_ids, memory_usage = _memory_focus(str(retrieval["topic"]))
    question, override = _single_question(str(retrieval["query"]), ordinal)
    anchor = retrieval["source_anchor"]
    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "case_id": f"answer-single-{ordinal:03d}",
        "fixture_id": FIXTURE_ID,
        "case_type": "single_turn",
        "description": f"单轮回答：{retrieval['topic']}",
        "tags": [
            "single_turn",
            str(chunks[retrieval["primary_chunk_id"]]["grade_level"]).lower(),
            str(anchor["content_role"]),
            *retrieval["slices"],
        ],
        "conversation": [{"role": "user", "content": question}],
        "target_user_message_index": 0,
        "source": _case_source(retrieval, chunks),
        "expected": _expected(
            retrieval,
            memory_ids=memory_ids,
            memory_usage=memory_usage,
            current_overrides_memory=override,
            multi_turn=False,
        ),
    }


def _multi_conversation(
    retrieval: dict[str, Any], ordinal: int
) -> tuple[list[dict[str, str]], str, bool]:
    """生成固定历史前缀；目标追问始终要求完整回答原始教材问题。"""
    query = str(retrieval["query"])
    group = (ordinal - 1) // 8
    if group == 0:
        conversation = [
            {"role": "user", "content": query},
            {"role": "assistant", "content": "这个问题需要结合教材中的严格表述回答。"},
            {"role": "user", "content": "那你刚才说的严格表述具体是什么？请把原问题完整回答。"},
        ]
        return conversation, "pronoun_resolution", False
    if group == 1:
        conversation = [
            {"role": "user", "content": query},
            {"role": "assistant", "content": "回答时需要区分研究对象、必要条件和最终结论。"},
            {"role": "user", "content": "那请把原问题中的对象、条件和结论完整说清楚。"},
        ]
        return conversation, "condition_followup", False
    if group == 2:
        conversation = [
            {"role": "user", "content": query},
            {"role": "assistant", "content": "关键数学表达需要和符号含义、适用条件一起说明。"},
            {"role": "user", "content": "请继续，把原问题需要的定义或公式完整写出，并说明符号和条件。"},
        ]
        return conversation, "formula_followup", False
    if group == 3:
        conversation = [
            {"role": "user", "content": query},
            {"role": "assistant", "content": "可以按照教材中的关键步骤和逻辑关系逐层解释。"},
            {"role": "user", "content": "不要只概括，请分步骤把原问题完整解释清楚。"},
        ]
        return conversation, "why_followup", False
    # 最后 8 条用于当前请求覆盖共享 Memory，以及更长的三轮上下文。
    if ordinal <= 36:
        conversation = [
            {"role": "user", "content": query},
            {"role": "assistant", "content": "通常可以按直观意义、严格表述和例子三个层次说明。"},
            {"role": "user", "content": "这次不要用图像或例子，只按教材严格回答原问题。"},
        ]
        return conversation, "current_request_override", True
    conversation = [
        {"role": "user", "content": query},
        {"role": "assistant", "content": "先明确概念对象，再核对条件、数学表达和结论。"},
        {"role": "user", "content": "我最容易混淆的是条件和结论。"},
        {"role": "assistant", "content": "最终回答应把两部分分开，并指出容易误用的边界。"},
        {"role": "user", "content": "好，请完整回答最开始的问题，并提醒最容易混淆的地方。"},
    ]
    return conversation, "long_context_followup", False


def _build_multi(retrieval: dict[str, Any], ordinal: int, chunks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    memory_ids, memory_usage = _memory_focus(str(retrieval["topic"]))
    conversation, category, override = _multi_conversation(retrieval, ordinal)
    anchor = retrieval["source_anchor"]
    return {
        "schema_version": CASE_SCHEMA_VERSION,
        "case_id": f"answer-multi-{ordinal:03d}",
        "fixture_id": FIXTURE_ID,
        "case_type": "multi_turn",
        "description": f"多轮追问回答：{retrieval['topic']}（{category}）",
        "tags": [
            "multi_turn",
            category,
            str(chunks[retrieval["primary_chunk_id"]]["grade_level"]).lower(),
            str(anchor["content_role"]),
            *retrieval["slices"],
        ],
        "history_mode": "canonical_prefix",
        "conversation": conversation,
        "target_user_message_index": len(conversation) - 1,
        "source": _case_source(retrieval, chunks),
        "expected": _expected(
            retrieval,
            memory_ids=memory_ids,
            memory_usage=memory_usage,
            current_overrides_memory=override,
            multi_turn=True,
        ),
    }


def _artifact_anchor(chunk: dict[str, Any]) -> dict[str, Any]:
    refs = chunk.get("source_refs") or []
    source_pdf = refs[0].get("source_pdf") if refs else None
    return {
        "book_id": chunk["book_id"],
        "book_name": chunk["book_name"],
        "source_pdf": source_pdf,
        "page_start": chunk["source_page_start"],
        "page_end": chunk["source_page_end"],
        "chapter_path": chunk["chapter_path"],
        "content_role": chunk["content_role"],
        "chunk_index": chunk["chunk_index"],
        "source_hash": chunk["source_hash"],
        "content_hash": chunk["content_hash"],
    }


def _validate(cases: list[dict[str, Any]]) -> None:
    """静态校验数量、共享 Fixture、对话目标和教材锚点。"""
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if fixture.get("fixture_id") != FIXTURE_ID:
        raise ValueError("共享 Fixture ID 不一致")
    if len(cases) != 100:
        raise ValueError(f"Answer Eval Case 必须恰好 100 条，实际 {len(cases)}")
    counts = Counter(case["case_type"] for case in cases)
    if counts != {"single_turn": 60, "multi_turn": 40}:
        raise ValueError(f"单轮/多轮数量错误：{dict(counts)}")
    ids = [str(case["case_id"]) for case in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("case_id 重复")

    chunks = {str(row["chunk_id"]): row for row in _read_jsonl(CHUNKS_PATH)}
    for case in cases:
        if case.get("fixture_id") != FIXTURE_ID:
            raise ValueError(f"{case['case_id']} 未引用统一 Fixture")
        if "memory" in case or "graph_states" in case:
            raise ValueError(f"{case['case_id']} 不允许携带 Case 级 Memory/图谱状态")
        conversation = case.get("conversation")
        target = case.get("target_user_message_index")
        if not isinstance(conversation, list) or not conversation:
            raise ValueError(f"{case['case_id']} conversation 不能为空")
        if not isinstance(target, int) or not (0 <= target < len(conversation)):
            raise ValueError(f"{case['case_id']} target_user_message_index 非法")
        if conversation[target].get("role") != "user":
            raise ValueError(f"{case['case_id']} target 必须指向 user 消息")
        source = case["source"]
        primary_id = str(source["primary_chunk_id"])
        if primary_id not in chunks:
            raise ValueError(f"{case['case_id']} primary chunk 不存在：{primary_id}")
        acceptable = source["acceptable_chunk_ids"]
        if primary_id not in acceptable:
            raise ValueError(f"{case['case_id']} acceptable 必须包含 primary")
        if any(str(chunk_id) not in chunks for chunk_id in acceptable):
            raise ValueError(f"{case['case_id']} acceptable chunk 存在缺失")
        actual_anchor = _artifact_anchor(chunks[primary_id])
        if source["source_anchor"] != actual_anchor:
            raise ValueError(f"{case['case_id']} source_anchor 与 chunk artifact 不一致")
        expected = case["expected"]
        if expected["shared_memory"]["fixture_id"] != FIXTURE_ID:
            raise ValueError(f"{case['case_id']} expected Fixture ID 不一致")
        if not expected["answer_rubric"]["critical_source_excerpt"].strip():
            raise ValueError(f"{case['case_id']} 缺少教材参考片段")


def _schema() -> dict[str, Any]:
    """输出供人工和后续 Runner 复用的简化 JSON Schema。"""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "answer-eval-case-v1",
        "title": "Answer Eval Case v1",
        "type": "object",
        "required": [
            "schema_version",
            "case_id",
            "fixture_id",
            "case_type",
            "conversation",
            "target_user_message_index",
            "source",
            "expected",
        ],
        "properties": {
            "schema_version": {"const": CASE_SCHEMA_VERSION},
            "case_id": {"type": "string", "pattern": "^answer-(single|multi)-[0-9]{3}$"},
            "fixture_id": {"const": FIXTURE_ID},
            "case_type": {"enum": ["single_turn", "multi_turn"]},
            "conversation": {"type": "array", "minItems": 1},
            "target_user_message_index": {"type": "integer", "minimum": 0},
            "source": {"type": "object"},
            "expected": {"type": "object"},
        },
        "additionalProperties": True,
    }


def main() -> None:
    """生成 60 条单轮和 40 条多轮 Case，并执行静态数据校验。"""
    retrieval = _read_jsonl(RETRIEVAL_CASES_PATH)
    if len(retrieval) < 70:
        raise ValueError(f"至少需要 70 条 Retrieval Case，实际 {len(retrieval)}")
    chunks = {str(row["chunk_id"]): row for row in _read_jsonl(CHUNKS_PATH)}
    cases = [
        *[_build_single(row, index, chunks) for index, row in enumerate(retrieval[:60], start=1)],
        *[_build_multi(row, index, chunks) for index, row in enumerate(retrieval[30:70], start=1)],
    ]
    _validate(cases)
    OUTPUT_PATH.write_text(
        "".join(json.dumps(case, ensure_ascii=False) + "\n" for case in cases),
        encoding="utf-8",
    )
    SCHEMA_PATH.write_text(
        json.dumps(_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已生成 {len(cases)} 条 Answer Eval Case：{OUTPUT_PATH}")
    print("分布：single_turn=60, multi_turn=40；未运行任何模型或测试。")


if __name__ == "__main__":
    main()
