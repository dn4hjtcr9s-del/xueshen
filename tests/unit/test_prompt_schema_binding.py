"""prompt 版本与文档 schema 绑定校验的单元测试（memory-rebuild §3.6④）。

覆盖矩阵：v2 文档 + v2 planner 通过；v2 文档 + v1 planner 抛错；v1 文档 + v1 planner 通过。
校验函数必须保持纯函数：不读文件、不连数据库。
"""

from __future__ import annotations

import ast
import inspect
from textwrap import dedent

import pytest

from backend.memory.graph.prompt_loader import (
    BUILD_MUTATION_PLAN_PROMPT_VERSION,
    DOCUMENT_SCHEMA_VERSION,
    REQUIRED_PLANNER_PROMPT_MAJOR,
    _planner_prompt_major_version,
    load_prompt,
    validate_schema_prompt_binding,
)

# schema v2 文档必须搭配的 planner prompt 版本（§3.6① 交付物）。
PLANNER_V2 = "build_mutation_plan_v2"


def test_v2_document_with_v2_planner_passes() -> None:
    """v2 文档 + v2 planner：合法组合。"""
    assert validate_schema_prompt_binding(2, planner_prompt_version=PLANNER_V2) is None


def test_v2_document_with_v1_planner_raises() -> None:
    """v2 文档 + v1 planner：配置错误，必须抛中文异常的 ValueError。"""
    with pytest.raises(ValueError) as excinfo:
        validate_schema_prompt_binding(2, planner_prompt_version="build_mutation_plan_v1")
    message = str(excinfo.value)
    assert "schema_version=2" in message
    assert "build_mutation_plan_v1" in message
    assert "build_mutation_plan_v2" in message  # 修复方式写在异常消息里
    assert "配置错误" in message


def test_v1_document_with_v1_planner_passes() -> None:
    """v1 文档 + v1 planner：历史版本永不原地迁移，继续合法（§2.7 决议 A 组）。"""
    assert (
        validate_schema_prompt_binding(1, planner_prompt_version="build_mutation_plan_v1") is None
    )


def test_v1_document_with_v2_planner_passes() -> None:
    """v1 文档 + v2 planner：新 prompt 同时覆盖 v1 写入纪律，允许。"""
    assert validate_schema_prompt_binding(1, planner_prompt_version=PLANNER_V2) is None


def test_later_planner_versions_also_accepted() -> None:
    """绑定按主版本号判定，v3 及以后同样满足 v2 文档。"""
    assert (
        validate_schema_prompt_binding(2, planner_prompt_version="build_mutation_plan_v3") is None
    )


def test_defaults_read_current_configuration() -> None:
    """不传参数时按**当前**配置自检，且当前配置必须是自洽的。

    Phase 4 把 planner 常量从 v1 升到 v2 之后，默认配置就是"v2 文档 + v2 planner"，
    校验必须通过。这里同时钉住三者的一致性：一旦有人只改了文档版本而忘了升 planner
    （或反之），本用例会立刻失败。
    """
    assert DOCUMENT_SCHEMA_VERSION == 2
    assert BUILD_MUTATION_PLAN_PROMPT_VERSION == "build_mutation_plan_v2"
    assert REQUIRED_PLANNER_PROMPT_MAJOR == 2
    validate_schema_prompt_binding()
    # 显式传 None 等价于使用 DOCUMENT_SCHEMA_VERSION
    validate_schema_prompt_binding(None)


def test_explicit_none_uses_document_schema_version() -> None:
    """None 表示"未读到具体文档"，按当前 DOCUMENT_SCHEMA_VERSION 判定。"""
    with pytest.raises(ValueError):
        validate_schema_prompt_binding(None, planner_prompt_version="build_mutation_plan_v1")
    assert validate_schema_prompt_binding(None, planner_prompt_version=PLANNER_V2) is None


def test_unknown_prompt_version_suffix_raises() -> None:
    """版本名缺少 `_v{N}` 后缀属于配置错误，同样抛 ValueError。"""
    with pytest.raises(ValueError, match="主版本号"):
        validate_schema_prompt_binding(2, planner_prompt_version="build_mutation_plan")


def test_validation_is_pure_and_lightweight() -> None:
    """启动校验必须无副作用：只比较版本号，不读文件、不建连接、不引入 IO 模块。"""

    def body_source(func: object) -> str:
        """取函数体（跳过 docstring）并反解析为源码，避免 docstring 误伤断言。"""
        module = ast.parse(dedent(inspect.getsource(func)))
        func_def = next(node for node in ast.walk(module) if isinstance(node, ast.FunctionDef))
        statements = [
            node
            for node in func_def.body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))
        ]
        return "\n".join(ast.unparse(node) for node in statements)

    body = body_source(validate_schema_prompt_binding)
    for forbidden in ("open(", "read_text", "read_bytes", "Path(", "session", "connect"):
        assert forbidden not in body
    # 只允许依赖纯计算辅助函数，禁止在启动校验里加载 Prompt 文件
    assert "load_prompt" not in body
    assert not inspect.iscoroutinefunction(validate_schema_prompt_binding)
    # 辅助函数同样保持纯计算
    helper_body = body_source(_planner_prompt_major_version)
    assert "open(" not in helper_body
    assert "Path(" not in helper_body


def test_v2_prompts_are_loadable_and_keep_v1_constraints() -> None:
    """两个新提示词可加载，且保留 v1/v2 既有硬约束（防止升级时丢约束）。"""
    plan_prompt = load_prompt(PLANNER_V2)
    extract_prompt = load_prompt("extract_candidates_v3")
    assert "expected_version" in plan_prompt  # §9.2 禁止模型生成并发令牌
    assert "一个事实只进它主体的文档" in plan_prompt  # §3.2 taxonomy 铁律
    assert "悬空链接" in plan_prompt
    assert "frontmatter_patch" in plan_prompt
    assert "related_topic_hints" in extract_prompt
    assert "主体归属" in extract_prompt
    assert "用户真实表现" in extract_prompt  # §9.4 区分助手陈述与用户表现
    assert "```json" in extract_prompt  # 保留"禁止 Markdown 围栏"约束
