"""生成知识总结 Phase 6 的 200 条双标注离线评测样本。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

OUTPUT = Path(__file__).with_name("knowledge_summary_cases_v1.jsonl")


def _id(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"xueshen:knowledge-summary:{label}"))


def _content() -> dict[str, object]:
    return {
        "schema_version": 1,
        "overview": None,
        "definitions": [],
        "theorems": [],
        "formulas": [],
        "properties": [],
        "methods": [],
        "pitfalls": [],
    }


def _summary(label: str, key: str, *, protected: list[str] | None = None) -> dict[str, object]:
    return {
        "summary_id": _id(f"summary:{label}"),
        "version": 2,
        "state_hash": hashlib.sha256(key.encode()).hexdigest(),
        "content": _content(),
        "normalized_topic_group": key.split("/", 1)[0],
        "normalized_topic_title": key.split("/", 1)[1],
        "protected_sections": protected or [],
    }


def _case(family: str, variant: int) -> dict[str, object]:
    label = f"{family}:{variant:03d}"
    user_message_id = _id(f"message:{label}:user")
    assistant_message_id = _id(f"message:{label}:assistant")
    topic_group, topic_title, section, statement = {
        "definition": (
            "函数",
            "连续函数",
            "definitions",
            "在区间上连续的函数满足局部极限等于函数值。",
        ),
        "formula": ("圆锥曲线", "椭圆的离心率", "formulas", "椭圆离心率满足 e=c/a，且 0<e<1。"),
        "theorem": (
            "微积分",
            "拉格朗日中值定理",
            "theorems",
            "闭区间连续且开区间可导的函数存在一点使导数等于平均变化率。",
        ),
        "method": ("数列", "单调有界法", "methods", "证明数列收敛时可以先证明单调，再证明有界。"),
        "pitfall": (
            "概率",
            "条件概率与独立性",
            "pitfalls",
            "条件概率不等于独立性，必须验证条件概率是否等于无条件概率。",
        ),
    }[family]
    key = f"{topic_group}/{topic_title}"
    messages = [
        {
            "message_id": user_message_id,
            "role": "user",
            "sequence": 1,
            "content": f"请总结第 {variant} 组关于{topic_title}的数学知识，并说明关键结论。",
        },
        {
            "message_id": assistant_message_id,
            "role": "assistant",
            "sequence": 2,
            "content": f"核心结论是：{statement}",
        },
    ]
    base: dict[str, object] = {
        "case_id": f"ks-{family}-{variant:03d}",
        "split": "dev" if variant < 12 else "validation" if variant < 16 else "test",
        "tags": [family, "mathematics"],
        "messages": messages,
        "existing_summaries": [],
    }
    candidate = {
        "target_summary_key": key,
        "topic_group_title": topic_group,
        "topic_title": topic_title,
        "sections": {section: [statement]},
        "source_message_ids": [assistant_message_id],
        "action": "create",
    }
    base["gold"] = {
        "should_generate": True,
        "candidates": [candidate],
        "expected_action": "create",
        "target_summary_key": key,
    }
    return base


def _special_case(kind: str, variant: int) -> dict[str, object]:
    label = f"{kind}:{variant:03d}"
    user_id = _id(f"message:{label}:user")
    assistant_id = _id(f"message:{label}:assistant")
    key = "圆锥曲线/椭圆的离心率"
    statement = "椭圆离心率满足 e=c/a，且 0<e<1。"
    messages = [
        {
            "message_id": user_id,
            "role": "user",
            "sequence": 1,
            "content": "请判断这段内容是否需要更新我的数学知识总结。",
        },
        {
            "message_id": assistant_id,
            "role": "assistant",
            "sequence": 2,
            "content": statement,
        },
    ]
    existing: list[dict[str, object]] = []
    if kind in {"merge", "no_change", "review", "protected"}:
        existing.append(
            _summary(
                f"{label}:target",
                key,
                protected=["formulas"] if kind == "protected" else None,
            )
        )
    if kind == "review":
        existing.append(_summary(f"{label}:alias", "圆锥曲线/椭圆偏心率"))
    if kind == "non_math":
        case: dict[str, object] = {
            "case_id": f"ks-{kind}-{variant:03d}",
            "split": "dev" if variant < 12 else "validation" if variant < 16 else "test",
            "tags": ["non_math", "off_topic"],
            "messages": [
                {
                    "message_id": user_id,
                    "role": "user",
                    "sequence": 1,
                    "content": "帮我写一封周末聚会邀请，不需要数学总结。",
                },
                {
                    "message_id": assistant_id,
                    "role": "assistant",
                    "sequence": 2,
                    "content": "当然可以，邀请内容应包含时间、地点和回复方式。",
                },
            ],
            "existing_summaries": [],
        }
        gold = {
            "should_generate": False,
            "candidates": [],
            "expected_action": "no_change",
            "target_summary_key": None,
        }
        case["gold"] = gold
    else:
        action = {
            "merge": "merge",
            "no_change": "no_change",
            "review": "needs_review",
            "protected": "needs_review",
        }[kind]
        candidate = {
            "target_summary_key": key,
            "topic_group_title": "圆锥曲线",
            "topic_title": "椭圆的离心率",
            "sections": {"formulas": [statement]},
            "source_message_ids": [assistant_id],
            "action": action,
        }
        case = {
            "case_id": f"ks-{kind}-{variant:03d}",
            "split": "dev" if variant < 12 else "validation" if variant < 16 else "test",
            "tags": [kind, "mathematics"],
            "messages": messages,
            "existing_summaries": existing,
            "gold": {
                "should_generate": True,
                "candidates": [candidate],
                "expected_action": action,
                "target_summary_key": key,
            },
        }
    # 生成器只提供覆盖面与 gold 草案；双人独立标注和仲裁必须由人工补齐。
    case["annotation_state"] = "pending_human_double_annotation"
    return case


def main() -> None:
    """生成 200 条固定、可重复的双标注样本。"""
    cases: list[dict[str, object]] = []
    for family in ("definition", "formula", "theorem", "method", "pitfall"):
        for variant in range(20):
            cases.append(_case(family, variant))
    for kind in ("merge", "no_change", "review", "protected", "non_math"):
        for variant in range(20):
            cases.append(_special_case(kind, variant))
    for case in cases:
        # 不能用程序生成的相同答案伪造“双人独立标注”。
        case["annotation_state"] = "pending_human_double_annotation"
    OUTPUT.write_text(
        "".join(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n" for case in cases),
        encoding="utf-8",
    )
    print(f"写入 {len(cases)} 条评测样本：{OUTPUT}")


if __name__ == "__main__":
    main()
