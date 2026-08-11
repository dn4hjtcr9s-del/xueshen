"""依据章节、元素类型和数学文本前缀分类内容角色及排除原因。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from scripts.embedding_chunks.schemas import CleanRecord, ContentRole

ANSWER_PAT = re.compile(r"(部分)?习题(参考)?答案|参考答案|答案与提示")
NON_CONTENT_PAT = re.compile(r"参考文献|索引|中英文名词|名词索引")
APPENDIX_PAT = re.compile(r"^附录")
DEFINITION_PAT = re.compile(r"^(定义|公理)\s*[\d一二三四五六七八九十.．-]*")
THEOREM_PAT = re.compile(r"^(定理|引理|推论|命题|法则)\s*[\d一二三四五六七八九十.．-]*")
PROOF_PAT = re.compile(r"^证明(?:\s*[:：.]|\s|$)")
EXAMPLE_PAT = re.compile(r"^例\s*[\d一二三四五六七八九十.．-]+")
SOLUTION_PAT = re.compile(r"^解(?:答)?\s*[:：.]?")
EXERCISE_PAT = re.compile(
    r"^(习题|练习|复习题|测试题|探究|思考|观察|做一做|想一想|试一试|复习巩固|综合运用|拓广探索)"
)


@dataclass(frozen=True, slots=True)
class Classification:
    """单条 clean record 的分类结果。"""

    role: ContentRole | None
    retrieval_weight: float = 1.0
    exclusion_reason: str | None = None


def classify_record(record: CleanRecord, chapter_path: tuple[str, ...]) -> Classification:
    """返回内容角色；明确排除时 role 为 None 并携带稳定 reason code。"""
    path_text = " > ".join(chapter_path)
    text = record.text.strip()

    if record.element_type == "title":
        return Classification(role=None, exclusion_reason="heading_metadata")
    if record.section == "front_matter":
        return Classification(role=None, exclusion_reason="front_matter")
    if NON_CONTENT_PAT.search(path_text) or NON_CONTENT_PAT.match(text):
        return Classification(role=None, exclusion_reason="non_content_back_matter")
    if ANSWER_PAT.search(path_text) or (
        record.section == "back_matter" and ANSWER_PAT.match(text)
    ):
        return Classification(role="answer_key", retrieval_weight=0.65)
    if APPENDIX_PAT.search(path_text):
        return Classification(role="appendix")

    if record.element_type in {"image", "chart"}:
        caption = str(record.extra.get("caption", "")).strip()
        if not caption:
            return Classification(role=None, exclusion_reason="image_without_caption")
        return Classification(role="figure_caption")
    if record.element_type == "table":
        return Classification(role="table")
    if record.element_type == "equation_interline":
        return Classification(role="formula")
    if not text:
        return Classification(role=None, exclusion_reason="empty_text")

    if DEFINITION_PAT.match(text):
        return Classification(role="definition")
    if THEOREM_PAT.match(text):
        return Classification(role="theorem")
    if PROOF_PAT.match(text):
        return Classification(role="proof")
    if EXAMPLE_PAT.match(text):
        return Classification(role="example")
    if SOLUTION_PAT.match(text):
        return Classification(role="solution")
    if EXERCISE_PAT.match(text):
        return Classification(role="exercise")
    return Classification(role="body")
