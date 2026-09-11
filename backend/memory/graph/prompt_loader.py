"""Prompt 加载与版本管理（§9.4）。

Prompt 文件独立版本管理，名称包含版本号；每次 LLM 调用记录 prompt_version。
日志不记录完整 Prompt、原始对话和完整模型输出。
另含文档 schema 与 planner prompt 的绑定校验（§3.6④）：schema v2 文档必须配 v2 planner。
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).parent / "prompts"

EXTRACT_CANDIDATES_PROMPT_VERSION = "extract_candidates_v3"
BUILD_MUTATION_PLAN_PROMPT_VERSION = "build_mutation_plan_v2"

#: 当前长期记忆文档 frontmatter 的 schema 版本。
DOCUMENT_SCHEMA_VERSION = 2

#: 与 `DOCUMENT_SCHEMA_VERSION` 配套的最低 planner prompt 主版本（§3.6④）。
REQUIRED_PLANNER_PROMPT_MAJOR = 2

#: 版本名末尾的 `_v{N}` 主版本号。
_VERSION_SUFFIX_PATTERN = re.compile(r"_v(\d+)$")


@lru_cache(maxsize=8)
def load_prompt(prompt_version: str) -> str:
    """按版本名加载 Prompt 文件；版本名即文件名（不含 .md）。"""
    if not prompt_version or "/" in prompt_version or "\\" in prompt_version:
        raise ValueError(f"非法 prompt_version: {prompt_version!r}")
    path = PROMPTS_DIR / f"{prompt_version}.md"
    if not path.exists():
        raise FileNotFoundError(f"Prompt 文件不存在: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 文档 schema 与 planner prompt 的版本绑定（§3.6④）
# ---------------------------------------------------------------------------


def _planner_prompt_major_version(prompt_version: str) -> int:
    """取 prompt 版本名末尾 `_v{N}` 的主版本号；无法识别时抛 ValueError。"""
    matched = _VERSION_SUFFIX_PATTERN.search(prompt_version or "")
    if matched is None:
        raise ValueError(f"无法从 planner prompt 版本名解析主版本号: {prompt_version!r}")
    return int(matched.group(1))


def validate_schema_prompt_binding(
    document_schema_version: int | None = None,
    *,
    planner_prompt_version: str = BUILD_MUTATION_PLAN_PROMPT_VERSION,
) -> None:
    """启动校验（§3.6④）：文档 schema 与 planner prompt 版本必须配套。

    纯函数、无副作用：不读 Prompt 文件、不连数据库，可在导入后任意时刻调用。
    schema v1 文档继续配 v1 planner 合法（历史版本永不原地迁移，§2.7 决议 A 组）；
    schema >= v2 的文档配 v1 planner 视为配置错误，抛 `ValueError`。

    调用方式：在进程启动路径上、确定本次运行使用的文档 schema 版本后调用，
    例如 ``validate_schema_prompt_binding(DOCUMENT_SCHEMA_VERSION)``；
    不传参数时按当前配置（`DOCUMENT_SCHEMA_VERSION` 与
    `BUILD_MUTATION_PLAN_PROMPT_VERSION`）自检。
    """
    if document_schema_version is None:
        document_schema_version = DOCUMENT_SCHEMA_VERSION
    if document_schema_version < DOCUMENT_SCHEMA_VERSION:
        return
    if _planner_prompt_major_version(planner_prompt_version) >= REQUIRED_PLANNER_PROMPT_MAJOR:
        return
    raise ValueError(
        f"配置错误：文档 frontmatter 为 schema_version={document_schema_version}，"
        f"但 planner prompt 仍是 {planner_prompt_version!r}（要求主版本 >= "
        f"{REQUIRED_PLANNER_PROMPT_MAJOR}）。schema v2 的 frontmatter "
        "name/description/aliases 与 `[[link]]` 互链只有 v2 及以上 planner 能正确生成，"
        "混用会让新格式文档被旧规则改写；请把 build_mutation_plan prompt 升级为 "
        "build_mutation_plan_v2（同步更新 BUILD_MUTATION_PLAN_PROMPT_VERSION）后重启。"
    )
