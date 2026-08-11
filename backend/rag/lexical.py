"""中文 FTS 与公式索引预处理：生成稳定、可版本化的检索 token。"""

from __future__ import annotations

import re
import unicodedata

LEXICAL_PIPELINE_VERSION = "zh-bigram-formula/v1"

_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LATIN_TERM_RE = re.compile(r"[a-z0-9_]+")
_FORMULA_PATTERNS = (
    re.compile(r"\$\$(.+?)\$\$", re.DOTALL),
    re.compile(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", re.DOTALL),
    re.compile(r"\\\((.+?)\\\)", re.DOTALL),
    re.compile(r"\\\[(.+?)\\\]", re.DOTALL),
)


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower().strip()


def _deduplicate(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def extract_formula_terms(text: str) -> tuple[str, ...]:
    """抽取 LaTeX 行内/块公式，并去除不影响精确匹配的空白。"""
    normalized = _normalize(text)
    formulas: list[str] = []
    for pattern in _FORMULA_PATTERNS:
        for match in pattern.finditer(normalized):
            canonical = re.sub(r"\s+", "", match.group(1))
            if canonical:
                formulas.append(canonical)
    return _deduplicate(formulas)


def lexical_tokens(text: str) -> tuple[str, ...]:
    """为 PostgreSQL simple FTS 生成中文二元组和拉丁数字词元。"""
    normalized = _normalize(text)
    tokens: list[str] = []
    for run in _CJK_RUN_RE.findall(normalized):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    tokens.extend(_LATIN_TERM_RE.findall(normalized))
    return _deduplicate(tokens)


def build_search_text(text: str) -> str:
    """生成持久化到 chunks.search_text 的空格分隔词元串。"""
    return " ".join(lexical_tokens(text))


def build_tsquery(text: str) -> str:
    """生成只含已规范化词元的 OR tsquery，避免直接拼接用户原文。"""
    tokens = lexical_tokens(text)
    return " | ".join(f"'{token.replace("'", "''")}'" for token in tokens)
