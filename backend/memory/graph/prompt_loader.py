"""Prompt 加载与版本管理（§9.4）。

Prompt 文件独立版本管理，名称包含版本号；每次 LLM 调用记录 prompt_version。
日志不记录完整 Prompt、原始对话和完整模型输出。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"

EXTRACT_CANDIDATES_PROMPT_VERSION = "extract_candidates_v1"
BUILD_MUTATION_PLAN_PROMPT_VERSION = "build_mutation_plan_v1"


@lru_cache(maxsize=8)
def load_prompt(prompt_version: str) -> str:
    """按版本名加载 Prompt 文件；版本名即文件名（不含 .md）。"""
    if not prompt_version or "/" in prompt_version or "\\" in prompt_version:
        raise ValueError(f"非法 prompt_version: {prompt_version!r}")
    path = PROMPTS_DIR / f"{prompt_version}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")
    return path.read_text(encoding="utf-8")
