"""错误码「封闭集合」的枚举型元测试（DEV-062 同类项的根治）。

**为什么必须是元测试**：``backend/memory/contracts/errors.py::ERROR_CODES`` 是文档意义上的
"公开错误码全集"，但**从来没有人校验它**——它既不被生产代码引用，也没有测试绑定。于是
异常类可以随便新增一个 ``code`` 而不登记，登记进集合的 code 也可以永远没有生产者：

- ``BATCH_ALL_MEMBERS_FAILED``（评审新发现 12）：异常类存在、``public_error.code`` 会落到
  批次 operation 行上（对外可见），集合里却没有它；
- 同类漏项还有 ``ACCOUNT_PURGE_IN_PROGRESS``（本文件 ``AccountPurgeInProgressError``）与
  ``ACCOUNT_PURGE_NOT_DRAINED``（``services/account_purge.py``）；
- 跨域同型漏项：``backend/study`` 的 ``StudyRateLimitedError.code = "RATE_LIMITED"`` 同样
  不在 ``STUDY_ERROR_CODES`` 里（``§17`` 的 429 语义明确要求该码）。

逐点补测试只能证明"被点名的那一处"修好了，证明不了同类没有第二处。因此这里把**集合**与
**生产者**用 AST 全量枚举后双向绑定，规则如下（每条失败都会列出**全部**违规项）：

- **R1 异常类 code ⊆ 集合**：任何异常类自己声明的 ``code`` 必须登记进该域集合（内存域、
  Conversation、Community、Study 四套集合都查）；
- **R2 集合 ⊆ 生产者**：集合里的每个 code 都必须有生产者（异常类、``code`` 位置的字符串
  字面量，或本文件显式登记且**核对过生产者文件**的非异常生产者），否则是"永不抛出的死值"；
- **R3 没有自己 code 的异常类必须显式登记原因**：Memory 全域 + 其它三域的
  ``contracts/errors.py``；新增异常类要么带 code，要么在这里说明"为什么不带"；
- **R4 ``code`` 位置的字符串字面量 ⊆ 集合**：``{"code": "X"}`` / ``code="X"`` 形态绕过异常
  类直接写公开信封时，同样受集合约束；
- **R5 白名单是双向精确的**：每一条豁免都必须真的被枚举命中（否则是"过期豁免"），
  且非异常生产者豁免不得与真实生产者重复；
- **R6 集合常量必须是字面量**：运行时集合值必须与源码里的 ``frozenset({...})`` 字面量相等，
  防止有人改用动态拼装把元测试变成空转；
- **R7 app 外壳的公开码必须登记在任何一套域集合里**：``backend/app.py`` 直接产出的
  ``MAINTENANCE_MODE`` / ``AUDIT_WRITE_FAILED`` 不属于任何域集合，必须在此显式登记。

关于"登记 ERROR_CODES 有没有实际约束力"：本文件就是它**唯一**的约束力来源——见报告
（该集合在登记之前无人读取，纯文档；本元测试把它变成 CI 强制的双向绑定）。
"""

from __future__ import annotations

import ast
import importlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

#: 仓库根（本文件在 tests/unit/ 下）。
REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class DomainSpec:
    """一个域的「错误码封闭集合」：运行时模块 + 集合常量名 + 需要枚举的包。"""

    name: str
    module: str
    codes_attr: str
    package: str

    @property
    def errors_path(self) -> str:
        return self.module.replace(".", "/") + ".py"


DOMAINS: tuple[DomainSpec, ...] = (
    DomainSpec("memory", "backend.memory.contracts.errors", "ERROR_CODES", "backend/memory"),
    DomainSpec(
        "conversation",
        "backend.conversation.contracts.errors",
        "CONVERSATION_ERROR_CODES",
        "backend/conversation",
    ),
    DomainSpec(
        "community",
        "backend.community.contracts.errors",
        "COMMUNITY_ERROR_CODES",
        "backend/community",
    ),
    DomainSpec("study", "backend.study.contracts.errors", "STUDY_ERROR_CODES", "backend/study"),
)

_DOMAIN_BY_NAME: dict[str, DomainSpec] = {spec.name: spec for spec in DOMAINS}

#: 四个域集合的并集（R7 用：app 外壳产出的码可能属于任何一域）。
ALL_DOMAIN_CODES: frozenset[str] = frozenset().union(
    *(getattr(importlib.import_module(spec.module), spec.codes_attr) for spec in DOMAINS)
)

#: 共享 ``PublicError`` 信封的兜底码：各域基类 ``code: str = "INTERNAL_ERROR"`` 的默认值。
#: 它登记在 Memory 域 ``ERROR_CODES`` 里（app.py 用同一个信封），不算各域"自己"的码。
_SHARED_ENVELOPE_REASON = (
    "共享 PublicError 信封的兜底码（登记在 Memory 域 ERROR_CODES，app.py 用同一个信封）："
    '本域基类 ``code: str = "INTERNAL_ERROR"`` 的默认值，不是本域错误码清单的一员。'
)

#: R1 的显式豁免：(域, code) → 原因。空集是目标状态——有豁免就说明"异常类带了一个本域
#: 集合里没有的码"。当前只有三域基类继承下来的共享兜底码。
EXCEPTION_CODE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("conversation", "INTERNAL_ERROR"): _SHARED_ENVELOPE_REASON,
    ("community", "INTERNAL_ERROR"): _SHARED_ENVELOPE_REASON,
    ("study", "INTERNAL_ERROR"): _SHARED_ENVELOPE_REASON,
}

#: R2 的显式豁免：(域, code) → (原因, 生产者文件)。生产者文件里必须真的出现该字面量
#: （本文件会核对），避免豁免变成"随口一说"。
AUTH_VERIFIER = "backend/auth/verifier.py"
NON_CLASS_PRODUCERS: dict[tuple[str, str], tuple[str, str]] = {
    ("memory", "AUTH_REQUIRED"): (
        '认证验签器用 ``AuthError("AUTH_REQUIRED", ...)`` 直接构造（多个分支），'
        "不走 Memory 域异常层级，因此没有对应的异常类。",
        AUTH_VERIFIER,
    ),
    ("memory", "AUTH_FORBIDDEN"): (
        '同上：scope/actor 不足时由 ``AuthError("AUTH_FORBIDDEN", ...)`` 直接构造。',
        AUTH_VERIFIER,
    ),
}

#: R4 的显式豁免：(域, code) → 原因。用于"确实写在 ``code`` 位置、但有意不进公开信封"的码。
CODE_POSITION_ALLOWLIST: dict[tuple[str, str], str] = {
    ("memory", "LLM_BUDGET_EXHAUSTED"): (
        "图 state 内部错误码：写进 ``state['errors']`` 参与 dead_letter/needs_review 判定，"
        "不出现在 PublicError 信封里（公开面只有 warnings），因此不进 ERROR_CODES。"
    ),
}

#: R3 登记：没有自己 ``code`` 的异常类 → 原因。键是 ``相对仓库根的模块路径::类名``。
#: Memory 域枚举整个包；其它三域只枚举各自 ``contracts/errors.py``（域的公开错误模块）。
NO_PUBLIC_CODE_EXCEPTIONS: dict[str, str] = {
    "backend/memory/backup.py::BackupError": (
        "备份/恢复 CLI 的工具级错误：只经 CLI 退出码与日志暴露，不经过 API 的 PublicError 信封。"
    ),
    "backend/memory/client.py::MemoryClientError": (
        "MemoryClient SDK 的客户端错误：``code`` 来自服务端响应（构造函数参数），"
        "不是类级固定值，因此无法登记为类 code。"
    ),
    "backend/memory/contracts/batch.py::BatchContractError": (
        "批次契约自检（ValueError 子类）：构建/校验期即失败，调用方是内部代码与测试。"
    ),
    "backend/memory/contracts/common.py::TopicKeyError": (
        "topic_key 校验错误（ValueError）：API 层捕获后转 ``InvalidPayloadError``"
        "（公开码 INVALID_PAYLOAD）。"
    ),
    "backend/memory/contracts/errors.py::LeaseFencedError": (
        "内部执行信号（失租）：docstring 已写明不对应公开错误码，执行层必须立即终止旧执行者。"
    ),
    "backend/memory/graph/policies.py::LLMBudgetExceededError": (
        "图内部预算信号：被节点捕获后写进 ``state['errors']`` 的 LLM_BUDGET_EXHAUSTED"
        "（见 CODE_POSITION_ALLOWLIST），不进公开信封。"
    ),
    "backend/memory/knowledge_graph/parser.py::KnowledgeGraphParseError": (
        "知识图谱文件解析错误（ValueError）：注册表加载/CLI 同步期失败。"
    ),
    "backend/memory/maintenance_gate.py::MaintenanceGateError": (
        "维护门禁基类：由 app.py 中间件映射为公开码 MAINTENANCE_MODE"
        "（不属于任何域集合，见 APP_SHELL_ONLY_CODES）。"
    ),
    "backend/memory/maintenance_gate.py::MaintenanceActiveError": "同上：系统正处于全局维护状态。",
    "backend/memory/maintenance_gate.py::MaintenanceGateUnavailableError": (
        "同上：门禁状态无法可靠读取或锁无法可靠获取。"
    ),
    "backend/memory/maintenance_gate.py::RestoreAlreadyRunningError": (
        "同上：另一个恢复流程已持有全局恢复互斥锁。"
    ),
    "backend/memory/maintenance_gate.py::RestoreAbortedError": (
        "同上：恢复尚未写入目标，仅因安全前置条件不满足而主动中止。"
    ),
    "backend/memory/persistence/dangling_links.py::DanglingLinkError": (
        "悬空链接入参非法（ValueError）：consolidation 节点捕获后跳过坏条目，不进公开信封。"
    ),
    "backend/memory/storage/local_markdown.py::StoragePathError": (
        "本地 Markdown 存储的路径输入校验（ValueError）：内部调用与测试直接断言。"
    ),
    "backend/memory/storage/markdown_schema.py::MarkdownParseError": (
        "Markdown 解析错误（ValueError）：维护分支捕获后把文档记为不可解析，不进公开信封。"
    ),
    "backend/memory/worker/outbox_consumer.py::_DeliveryFencedError": (
        "outbox 投递失租的私有信号（下划线私有类）：只用于中断本次投递。"
    ),
    "backend/conversation/contracts/errors.py::StructuredOutputError": (
        "无自己的 code，继承 ``ModelUnavailableError`` 的 MODEL_UNAVAILABLE（已在集合内）："
        "它只补充诊断元数据。"
    ),
    "backend/conversation/contracts/errors.py::KnowledgeSummaryError": (
        "知识总结错误的分类基类：继承 ConversationError 的 INTERNAL_ERROR 兜底码，"
        "具体子类各自带 code。"
    ),
}

#: R7 登记：不属于任何域集合、但确实是公开信封码（app 外壳产出）→ (原因, 生产者文件)。
APP_SHELL_ONLY_CODES: dict[str, tuple[str, str]] = {
    "MAINTENANCE_MODE": (
        "全局维护门禁拒绝请求时的 503 码：由 app.py 中间件直接产出，"
        "不对应任何域异常类，也不在四套域集合里。",
        "backend/app.py",
    ),
    "AUDIT_WRITE_FAILED": (
        "break-glass 审计写入失败时的 500 码：同上，属 app 外壳的安全兜底。",
        "backend/app.py",
    ),
}

#: 枚举哨兵（防止提取器"静默空转"）：这些 (类名, code) 与 code 字面量必须被提取到。
SENTINEL_EXCEPTION_SENTINELS: tuple[tuple[str, str, str], ...] = (
    ("memory", "BatchAllMembersFailedError", "BATCH_ALL_MEMBERS_FAILED"),
    ("memory", "AccountPurgeNotDrainedError", "ACCOUNT_PURGE_NOT_DRAINED"),
    ("memory", "MemoryError", "INTERNAL_ERROR"),
    ("study", "StudyRateLimitedError", "RATE_LIMITED"),
)
SENTINEL_CODE_LITERAL = "OPERATION_DEAD_LETTER"


# ---------------------------------------------------------------------------
# AST 工具
# ---------------------------------------------------------------------------


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _relative(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """模块级字符串常量（``NAME = "..."`` / ``NAME: str = "..."``）。

    用于把 ``code = SOME_CONST`` 这种符号引用解析回字面量（``BatchAllMembersFailedError``
    就是这么写的）——解析不了会在 R1 里直接失败，不会静默漏项。
    """
    consts: dict[str, str] = {}
    for node in tree.body:
        target: str | None = None
        value: ast.expr | None = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target is not None and isinstance(value, ast.Constant) and isinstance(value.value, str):
            consts[target] = value.value
    return consts


def _base_names(node: ast.ClassDef) -> list[str]:
    """类定义里所有基类的"末段名字"（``Name`` / ``Attribute`` / ``Subscript`` 都认）。"""

    def name_of(expr: ast.expr) -> str:
        if isinstance(expr, ast.Name):
            return expr.id
        if isinstance(expr, ast.Attribute):
            return expr.attr
        if isinstance(expr, ast.Subscript):
            return name_of(expr.value)
        return ""

    return [name for base in node.bases if (name := name_of(base))]


def _looks_like_exception_class(node: ast.ClassDef) -> bool:
    """基类名以 ``Error`` / ``Exception`` 结尾即视为异常类（本仓库的命名约定）。"""
    return any(name.endswith(("Error", "Exception")) for name in _base_names(node))


def _own_code(node: ast.ClassDef, consts: dict[str, str], where: str) -> str | None:
    """类体里自己声明的 ``code``（含 ``AnnAssign``）；没有则 None。"""
    code: str | None = None
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "code" for target in stmt.targets
        ):
            value = stmt.value
        elif isinstance(stmt, ast.AnnAssign) and (
            isinstance(stmt.target, ast.Name) and stmt.target.id == "code"
        ):
            value = stmt.value
        else:
            continue
        if value is None:  # ``code: str``（纯注解，抽象基类）→ 视为"没有自己的 code"
            return None
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            code = value.value
        elif isinstance(value, ast.Name) and value.id in consts:
            code = consts[value.id]
        else:
            raise AssertionError(f"{where}: 无法在 AST 里解析 code 字面量: {ast.unparse(value)}")
    return code


@dataclass(frozen=True, slots=True)
class ExceptionClass:
    """一个被枚举到的异常类。"""

    domain: str
    module: str
    name: str
    code: str | None
    lineno: int

    @property
    def key(self) -> str:
        return f"{self.module}::{self.name}"

    def __str__(self) -> str:
        return f"{self.module}:{self.lineno} {self.name}(code={self.code!r})"


@dataclass(frozen=True, slots=True)
class CodeLiteral:
    """``code`` 位置上的一个字符串字面量（生产者站点）。"""

    domain: str
    module: str
    line: int
    value: str
    shape: str

    def __str__(self) -> str:
        return f"{self.module}:{self.line} [{self.shape}] {self.value}"


def _code_literals_in(path: Path, domain: str) -> list[CodeLiteral]:
    """收集一个模块里 ``code`` 位置的字面量：``{"code": "X"}`` 与 ``code="X"``。"""
    tree = _parse(path)
    consts = _module_string_constants(tree)
    module = _relative(path)
    found: list[CodeLiteral] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            # keys 里的 None 表示 ``**kwargs`` 展开（与 values 等长），因此不能用 strict=True
            for key, value in zip(node.keys, node.values, strict=False):
                if not (isinstance(key, ast.Constant) and key.value == "code"):
                    continue
                literal = _literal_or_const(value, consts)
                if literal is not None:
                    found.append(CodeLiteral(domain, module, value.lineno, literal, "dict"))
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg != "code":
                    continue
                literal = _literal_or_const(keyword.value, consts)
                if literal is not None:
                    found.append(
                        CodeLiteral(domain, module, keyword.value.lineno, literal, "kwarg")
                    )
    return found


def _literal_or_const(node: ast.expr, consts: dict[str, str]) -> str | None:
    """``"X"`` / ``CONST`` → 字面量；动态值（``exc.code`` 等）→ None（有意跳过）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and node.id in consts:
        return consts[node.id]
    return None


@cache
def _exception_classes(domain: str) -> tuple[ExceptionClass, ...]:
    """枚举一个域包内所有异常类（含"没有自己 code"的）。"""
    spec = _DOMAIN_BY_NAME[domain]
    found: list[ExceptionClass] = []
    for path in sorted((REPO_ROOT / spec.package).rglob("*.py")):
        tree = _parse(path)
        consts = _module_string_constants(tree)
        module = _relative(path)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or not _looks_like_exception_class(node):
                continue
            found.append(
                ExceptionClass(
                    domain=domain,
                    module=module,
                    name=node.name,
                    code=_own_code(node, consts, f"{module}::{node.name}"),
                    lineno=node.lineno,
                )
            )
    return tuple(found)


@cache
def _code_position_literals(domain: str) -> tuple[CodeLiteral, ...]:
    """枚举一个域包内所有 ``code`` 位置的字面量。"""
    spec = _DOMAIN_BY_NAME[domain]
    found: list[CodeLiteral] = []
    for path in sorted((REPO_ROOT / spec.package).rglob("*.py")):
        found.extend(_code_literals_in(path, domain))
    return tuple(found)


@cache
def _file_string_literals(relative_path: str) -> frozenset[str]:
    """一个文件里出现过的所有字符串字面量（用于核对豁免声明的生产者文件）。"""
    tree = _parse(REPO_ROOT / relative_path)
    return frozenset(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _collect_string_set(expr: ast.expr, where: str) -> set[str]:
    """把 ``frozenset({...})`` / ``{...}`` 字面量展开成字符串集合。"""
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name):
        assert len(expr.args) == 1, f"{where}: 集合构造调用必须是单参数"
        return _collect_string_set(expr.args[0], where)
    assert isinstance(expr, ast.Set | ast.List | ast.Tuple), (
        f"{where}: 错误码集合必须是字面量（frozenset({{...}}) / {{...}}），"
        f"实际是 {type(expr).__name__}"
    )
    values: set[str] = set()
    for element in expr.elts:
        assert isinstance(element, ast.Constant) and isinstance(element.value, str), (
            f"{where}: 集合成员必须是字符串字面量，实际是 {ast.unparse(element)}"
        )
        values.add(element.value)
    return values


@cache
def _source_set_literal(domain: str) -> frozenset[str]:
    """从源码 AST 里读出集合字面量。"""
    spec = _DOMAIN_BY_NAME[domain]
    tree = _parse(REPO_ROOT / spec.errors_path)
    for node in tree.body:
        target: str | None = None
        value: ast.expr | None = None
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target == spec.codes_attr and value is not None:
            return frozenset(_collect_string_set(value, f"{spec.errors_path}::{spec.codes_attr}"))
    raise AssertionError(f"{spec.errors_path}: 找不到集合常量 {spec.codes_attr}")


def _domain_codes(domain: str) -> frozenset[str]:
    """运行时集合值。"""
    spec = _DOMAIN_BY_NAME[domain]
    module = importlib.import_module(spec.module)
    return frozenset(getattr(module, spec.codes_attr))


# ---------------------------------------------------------------------------
# 枚举派生量（R5 白名单卫生用："实际观测到的"一侧）
# ---------------------------------------------------------------------------


@cache
def _observed_unregistered_exception_codes() -> dict[tuple[str, str], list[str]]:
    """R1 判定后**确实**落在集合外的 (域, code) → 触发它的异常类清单。"""
    observed: dict[tuple[str, str], list[str]] = {}
    for spec in DOMAINS:
        codes = _domain_codes(spec.name)
        for exc in _exception_classes(spec.name):
            if exc.code is None or exc.code in codes:
                continue
            observed.setdefault((spec.name, exc.code), []).append(str(exc))
    return observed


@cache
def _observed_unregistered_code_literals() -> dict[tuple[str, str], list[str]]:
    """R4 判定后**确实**落在集合外的 (域, code) → 站点清单。"""
    observed: dict[tuple[str, str], list[str]] = {}
    for spec in DOMAINS:
        codes = _domain_codes(spec.name)
        for literal in _code_position_literals(spec.name):
            if literal.value in codes:
                continue
            observed.setdefault((spec.name, literal.value), []).append(str(literal))
    return observed


@cache
def _observed_dead_codes() -> dict[tuple[str, str], list[str]]:
    """R2 判定后**确实**没有任何生产者的 (域, code) → 空列表（值为违规明细）。"""
    dead: dict[tuple[str, str], list[str]] = {}
    for spec in DOMAINS:
        codes = _domain_codes(spec.name)
        producers = {exc.code for exc in _exception_classes(spec.name) if exc.code is not None}
        producers |= {literal.value for literal in _code_position_literals(spec.name)}
        for code in sorted(codes - producers):
            dead[(spec.name, code)] = []
    return dead


@cache
def _observed_exception_classes_without_code() -> dict[str, str]:
    """R3 判定范围里**确实**没有自己 code 的异常类：Memory 全域 + 其它域 errors.py。"""
    observed: dict[str, str] = {}
    for spec in DOMAINS:
        classes = _exception_classes(spec.name)
        if spec.name != "memory":
            errors_module = spec.errors_path
            classes = tuple(exc for exc in classes if exc.module == errors_module)
        for exc in classes:
            if exc.code is None:
                observed[exc.key] = str(exc)
    return observed


# ---------------------------------------------------------------------------
# R6：集合常量必须是字面量（运行时值 == 源码字面量）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", DOMAINS, ids=[spec.name for spec in DOMAINS])
def test_domain_code_sets_are_plain_literals(spec: DomainSpec) -> None:
    """R6：运行时集合与源码 ``frozenset({...})`` 字面量必须相等。"""
    assert _domain_codes(spec.name) == _source_set_literal(spec.name), (
        f"{spec.errors_path}::{spec.codes_attr} 的运行时值与源码字面量不一致"
        "（集合被动态拼装了？元测试会因此空转）"
    )


# ---------------------------------------------------------------------------
# R1：异常类的 code ⊆ 集合
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", DOMAINS, ids=[spec.name for spec in DOMAINS])
def test_exception_codes_are_registered(spec: DomainSpec) -> None:
    """R1：异常类声明的 code 必须登记进该域集合（缺失项连类名一起列出）。"""
    problems = [
        f"{code}: " + "、".join(sorted(sites))
        for (domain, code), sites in sorted(_observed_unregistered_exception_codes().items())
        if domain == spec.name and (domain, code) not in EXCEPTION_CODE_ALLOWLIST
    ]
    assert not problems, (
        f"{spec.name} 域有异常类的 code 不在 {spec.codes_attr} 里：\n  " + "\n  ".join(problems)
    )


# ---------------------------------------------------------------------------
# R2：集合 ⊆ 生产者（死值必须显式白名单 + 原因）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", DOMAINS, ids=[spec.name for spec in DOMAINS])
def test_error_codes_have_producers(spec: DomainSpec) -> None:
    """R2：集合里不允许有"永不抛出"的死值（除非显式登记非异常生产者）。"""
    dead = [
        code
        for (domain, code) in sorted(_observed_dead_codes())
        if domain == spec.name and (domain, code) not in NON_CLASS_PRODUCERS
    ]
    assert not dead, (
        f"{spec.name} 域的 {spec.codes_attr} 里有没有任何生产者的死值："
        f"{dead}（要么补生产者，要么在 NON_CLASS_PRODUCERS 登记原因 + 生产者文件）"
    )


# ---------------------------------------------------------------------------
# R4：code 位置的字面量 ⊆ 集合
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", DOMAINS, ids=[spec.name for spec in DOMAINS])
def test_code_position_literals_are_registered(spec: DomainSpec) -> None:
    """R4：``{"code": "X"}`` / ``code="X"`` 写出的码同样必须登记（或显式豁免）。"""
    problems = [
        f"{code}: " + "、".join(sorted(sites))
        for (domain, code), sites in sorted(_observed_unregistered_code_literals().items())
        if domain == spec.name and (domain, code) not in CODE_POSITION_ALLOWLIST
    ]
    assert not problems, f"{spec.name} 域有 code 位置的未登记字面量：\n  " + "\n  ".join(problems)


# ---------------------------------------------------------------------------
# R3：没有自己 code 的异常类必须显式登记原因
# ---------------------------------------------------------------------------


def test_exception_classes_without_code_are_registered() -> None:
    """R3：新增一个不带 code 的异常类时，必须在这里说明"为什么不带"。"""
    observed = _observed_exception_classes_without_code()
    unregistered = sorted(key for key in observed if key not in NO_PUBLIC_CODE_EXCEPTIONS)
    assert not unregistered, (
        "以下异常类没有自己的 code，且未登记原因（要么补 code 进集合，"
        "要么在 NO_PUBLIC_CODE_EXCEPTIONS 说明为什么没有公开码）：\n  "
        + "\n  ".join(f"{key} -> {observed[key]}" for key in unregistered)
    )


# ---------------------------------------------------------------------------
# R5：白名单双向精确（过期豁免也要报）
# ---------------------------------------------------------------------------


def test_allowlists_are_exact() -> None:
    """R5：每条豁免都必须真的被枚举命中，避免"过期豁免"长期存活。"""
    problems: list[str] = []

    observed_exception_allow = set(_observed_unregistered_exception_codes())
    for key in sorted(set(EXCEPTION_CODE_ALLOWLIST) - observed_exception_allow):
        problems.append(f"EXCEPTION_CODE_ALLOWLIST 过期：{key} 已不再是集合外的异常类 code")
    for key in sorted(observed_exception_allow - set(EXCEPTION_CODE_ALLOWLIST)):
        problems.append(f"EXCEPTION_CODE_ALLOWLIST 缺少：{key}")

    observed_literals = set(_observed_unregistered_code_literals())
    for key in sorted(set(CODE_POSITION_ALLOWLIST) - observed_literals):
        problems.append(f"CODE_POSITION_ALLOWLIST 过期：{key} 已不再是未登记的 code 字面量")
    for key in sorted(observed_literals - set(CODE_POSITION_ALLOWLIST)):
        problems.append(f"CODE_POSITION_ALLOWLIST 缺少：{key}")

    observed_dead = set(_observed_dead_codes())
    for key in sorted(set(NON_CLASS_PRODUCERS) - observed_dead):
        problems.append(
            f"NON_CLASS_PRODUCERS 过期/多余：{key} 已经有真实生产者（异常类或 code 字面量）"
            "，或已不在该域集合里"
        )
    for key, (reason, producer) in sorted(NON_CLASS_PRODUCERS.items()):
        if key[1] not in _domain_codes(key[0]):
            problems.append(f"NON_CLASS_PRODUCERS 的 {key[1]} 不在 {key[0]} 域集合里")
        if key[1] not in _file_string_literals(producer):
            problems.append(
                f"NON_CLASS_PRODUCERS 的生产者证据不成立：{producer} 里找不到 {key[1]!r}"
                f"（声明的原因：{reason}）"
            )

    observed_no_code = set(_observed_exception_classes_without_code())
    for key in sorted(set(NO_PUBLIC_CODE_EXCEPTIONS) - observed_no_code):
        problems.append(f"NO_PUBLIC_CODE_EXCEPTIONS 过期：{key} 已经有自己的 code 或已不存在")

    for code, (_reason, producer) in sorted(APP_SHELL_ONLY_CODES.items()):
        if code in ALL_DOMAIN_CODES:
            problems.append(f"APP_SHELL_ONLY_CODES 的 {code} 已登记进某个域集合，应从这里删掉")
        if code not in _file_string_literals(producer):
            problems.append(
                f"APP_SHELL_ONLY_CODES 的生产者证据不成立：{producer} 里找不到 {code!r}"
            )

    assert not problems, "白名单不再精确：\n  " + "\n  ".join(problems)


# ---------------------------------------------------------------------------
# R7：app 外壳产出的公开码必须登记在任何一套域集合里
# ---------------------------------------------------------------------------

_APP_PATH = Path("backend/app.py")


def test_app_shell_public_codes_are_registered() -> None:
    """R7：``app.py`` 直接产出的公开码（``_public_error_body`` / ``{"code": ...}``）。

    ``MAINTENANCE_MODE`` / ``AUDIT_WRITE_FAILED`` 不属于任何域集合（它们是 app 外壳的
    安全兜底），必须显式登记——否则"ERROR_CODES 是公开码全集"就是一句空话。
    """
    tree = _parse(REPO_ROOT / _APP_PATH)
    consts = _module_string_constants(tree)
    produced: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name == "_public_error_body" and node.args:
                literal = _literal_or_const(node.args[0], consts)
                if literal is not None:
                    produced.add(literal)
    produced |= {literal.value for literal in _code_literals_in(REPO_ROOT / _APP_PATH, "app")}

    unregistered = sorted(produced - ALL_DOMAIN_CODES - set(APP_SHELL_ONLY_CODES))
    assert not unregistered, (
        f"{_APP_PATH} 产出了不属于任何域集合、也没登记的公开错误码：{unregistered}"
        "（要么登记进对应域集合，要么在 APP_SHELL_ONLY_CODES 说明原因）"
    )


# ---------------------------------------------------------------------------
# 枚举哨兵：防止提取器静默空转
# ---------------------------------------------------------------------------


def test_enumeration_sentinels() -> None:
    """自检：已知的异常类与 code 字面量必须被提取到，否则元测试是"空转的绿灯"。"""
    by_domain: dict[str, dict[str, str]] = {}
    for spec in DOMAINS:
        by_domain[spec.name] = {
            exc.name: exc.code for exc in _exception_classes(spec.name) if exc.code is not None
        }
    for domain, class_name, code in SENTINEL_EXCEPTION_SENTINELS:
        assert by_domain[domain].get(class_name) == code, (
            f"枚举器没有提取到 {domain} 域的 {class_name}(code={code})——提取器坏了"
        )
    literal_values = {
        literal.value for spec in DOMAINS for literal in _code_position_literals(spec.name)
    }
    assert SENTINEL_CODE_LITERAL in literal_values, (
        f"枚举器没有提取到 {SENTINEL_CODE_LITERAL} 这个 code 位置字面量——提取器坏了"
    )
