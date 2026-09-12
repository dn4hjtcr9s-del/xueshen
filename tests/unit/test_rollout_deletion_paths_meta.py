"""Rollout 删除路径的**全量枚举**元测试（review-2 新发现 6 / 15）。

**为什么要元测试**：删除一条 rollout 段要同时清掉**两份物理载体**（对象镜像 + 本地热段），
还要落 tombstone；三件事少一件就是"删了 thread，用户原文仍在磁盘上"。review 已经抓到两次
"修一处、漏同类"：C-3 只覆盖已封存段（漏了 ``object_key IS NULL`` 的未封存段）、kodo 后端
只删云端对象（漏了本地热缓存）。逐点补测试治不了下一次遗漏，因此这里做**全量枚举**：

1. :data:`DELETION_ENTRIES` 是**唯一**的删除入口清单；每个入口都必须是
   "删对象 + 删热段（逐 key，thread 级再加目录清扫）+ 落 tombstone（或显式拒绝）"三段齐全；
   ``retention-scan`` 是段级删除，**禁止**出现按 thread 清扫（会误删同 thread 未过期的段）；
2. **新入口自动暴露**：扫描 ``backend/conversation/**`` 全部函数，凡函数体**直接**调用
   物理删除原语或 ``delete_thread`` 墓碑的，必须在清单里登记，或在
   :data:`NON_ENTRY_ALLOWLIST` 里说明为什么不是入口；
3. **CLI 子命令守卫**：带 ``--apply`` 的子命令集合必须与登记表精确一致；
4. **集成覆盖守卫**：登记表里每个入口都必须在
   ``tests/integration/test_rollout_deletion_paths.py`` 里有对应的真库真盘用例。

两种检测口径刻意不同：

- **新入口检测**用"直接调用"（精确、零误报）——传递闭包会把
  ``run_forever`` / ``_poll_once`` / ``main`` 这些枢纽函数连成一张网，退化成白名单噪音；
- **三段齐全**用登记入口的传递闭包（严格、不误报）——确保入口最终真的走到三段助手。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

#: 生产端包根（相对仓库根）。
_CONVERSATION_ROOT = Path("backend/conversation")

#: 真库真盘参数化集成用例所在文件（集成覆盖守卫用）。
_INTEGRATION_TEST_FILE = Path("tests/integration/test_rollout_deletion_paths.py")


@dataclass(frozen=True, slots=True)
class DeletionEntry:
    """一个会删除 rollout 段/对象的入口。"""

    key: str
    """稳定标识：报告与集成测试用例名共用。"""

    label: str
    """中文说明（断言消息里用）。"""

    module: str
    """模块相对路径（仓库根起）。"""

    func: str
    """入口函数名。"""

    sweeps_thread: bool
    """是否必须按 ``<thread_id>`` 目录清扫热段（段级删除必须为 False）。"""

    tombstone: str
    """墓碑/拒绝语义的说明（人读；断言只检查标记函数是否可达）。"""

    cli_subcommand: str | None = None
    """对应的 CLI 子命令名（带 ``--apply`` 的那些必须登记）。"""


def _entry(
    key: str,
    label: str,
    module: str,
    func: str,
    *,
    sweeps_thread: bool,
    tombstone: str,
    cli_subcommand: str | None = None,
) -> DeletionEntry:
    return DeletionEntry(
        key=key,
        label=label,
        module=module,
        func=func,
        sweeps_thread=sweeps_thread,
        tombstone=tombstone,
        cli_subcommand=cli_subcommand,
    )


#: **唯一**的删除入口清单。新增删除路径必须在这里登记（否则第 2 条断言会变红）。
DELETION_ENTRIES: tuple[DeletionEntry, ...] = (
    _entry(
        "thread_deletion",
        "delete_thread Job：thread 删除的唯一协调器",
        "backend/conversation/services/thread_deletion.py",
        "execute_delete_thread",
        sweeps_thread=True,
        tombstone="mark_thread_deleted（缺对象存储/删除失败时显式拒绝：保持 deleting + 重试）",
    ),
    _entry(
        "cli_delete_thread_rollouts",
        "CLI delete-thread-rollouts --apply",
        "backend/conversation/cli/rollout.py",
        "_delete_thread_rollouts",
        sweeps_thread=True,
        tombstone="mark_thread_deleted（有失败项时显式拒绝：退出码 1，不落 tombstone）",
        cli_subcommand="delete-thread-rollouts",
    ),
    _entry(
        "cli_retention_scan",
        "CLI retention-scan --apply",
        "backend/conversation/cli/rollout.py",
        "_retention_scan",
        sweeps_thread=False,
        tombstone="mark_deleted（逐段；sealed 段缺 object_key 时显式失败，绝不静默跳过）",
        cli_subcommand="retention-scan",
    ),
)

#: 触发型入口：本身不删载荷，只负责把删除交给 ``delete_thread`` Job（唯一协调器）。
#: 断言它们**直接**没有调用任何删除原语，且源码里确实出现了 ``delete_thread``。
TRIGGER_ENTRIES: tuple[tuple[str, str, str], ...] = (
    (
        "API DELETE /conversations/{thread_id}",
        "backend/conversation/api/conversations.py",
        "delete_conversation",
    ),
    (
        "ConversationService.delete_thread",
        "backend/conversation/services/conversation_service.py",
        "delete_thread",
    ),
    (
        "JobWorker._run_delete_thread（Job 分派器）",
        "backend/conversation/worker/job_worker.py",
        "_run_delete_thread",
    ),
    (
        "JobWorker._execute_job（Job 类型分派）",
        "backend/conversation/worker/job_worker.py",
        "_execute_job",
    ),
)

#: 触及删除原语但**不是**删除入口的函数：``(模块名, 函数名) -> 原因``。
#: 新增项必须解释清楚"为什么它不负责删物理载荷"。同名函数（模块级 helper 与类方法）
#: 在按短名建图时会被合并，因此键里出现的方法名可能是"一族"。
NON_ENTRY_ALLOWLIST: dict[tuple[str, str], str] = {
    # rollout/deletion.py 的内部助手（被三个入口经统一助手调用）
    ("deletion.py", "delete_rollout_objects"): "内部助手：只删对象镜像，由段级助手组合",
    ("deletion.py", "delete_rollout_hot_segments"): "内部助手：只删逐 key 热段",
    ("deletion.py", "sweep_thread_hot_segments"): "内部助手：只做 thread 级热段清扫",
    ("deletion.py", "delete_rollout_payloads"): "段级统一助手（retention 经此路径）",
    ("deletion.py", "delete_thread_rollout_payloads"): "thread 级统一助手（Job 与 CLI 经此路径）",
    # rollout/object_store.py 的能力实现
    ("object_store.py", "delete_hot_segment"): (
        "能力分派 helper + Local/Fake/LocalHotSegmentCache 的实现方法（同名合并）"
    ),
    ("object_store.py", "delete_hot_thread"): "Local/Fake/LocalHotSegmentCache 的实现方法",
    ("object_store.py", "delete_hot_thread_segments"): "能力分派 helper（能力缺失返回 None）",
    # rollout/qiniu_kodo.py 的远端实现：转调本地热缓存能力，本身不是入口
    ("qiniu_kodo.py", "delete_hot_segment"): "Kodo 实现：转调本地热缓存能力",
    ("qiniu_kodo.py", "delete_hot_thread"): "Kodo 实现：转调本地热缓存能力",
}

#: **直接调用**口径的删除标记（新入口检测用）。
DIRECT_REMOVAL_MARKERS: frozenset[str] = frozenset(
    {
        "object_store.delete",
        "delete_rollout_payloads",
        "delete_thread_rollout_payloads",
        "delete_rollout_objects",
        "delete_rollout_hot_segments",
        "sweep_thread_hot_segments",
        "delete_hot_segment",
        "delete_hot_thread_segments",
        "delete_hot_segment_file",
        "delete_hot_thread_files",
        "delete_hot_thread",
        "mark_thread_deleted",
    }
)

#: **物理载荷删除**口径（触发入口的"不自己删数据"断言用）。
PAYLOAD_REMOVAL_MARKERS: frozenset[str] = frozenset(
    {
        "object_store.delete",
        "delete_rollout_objects",
        "delete_rollout_hot_segments",
        "sweep_thread_hot_segments",
        "delete_hot_segment",
        "delete_hot_thread_segments",
        "delete_hot_segment_file",
        "delete_hot_thread_files",
        "delete_hot_thread",
    }
)

#: 三段落地的必需标记。
REQUIRED_OBJECT_DELETION = "delete_rollout_objects"
REQUIRED_HOT_DELETION = "delete_rollout_hot_segments"
REQUIRED_THREAD_SWEEP = "sweep_thread_hot_segments"
TOMBSTONE_OR_REFUSAL_MARKERS = frozenset({"mark_thread_deleted", "mark_deleted", "wait_job"})

#: CLI 里带 ``--apply`` 但**不是**删除入口的子命令（必须写明原因）。
CLI_NON_ENTRY_MUTATIONS: dict[str, str] = {
    "reconcile-orphans": (
        "只把可安全修复的段标 tombstone；孤儿**对象**的删除明确留给人工"
        "（reconcile.repair 的授权边界），因此不经过载荷删除助手"
    ),
}


# ---------------------------------------------------------------------------
# AST 工具：函数级调用图
# ---------------------------------------------------------------------------


def _callee_name(node: ast.Call) -> str:
    """被调函数的短名；``<something_store>.delete(...)`` 归一成 ``object_store.delete``。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        if func.attr == "delete" and "store" in ast.unparse(func.value).lower():
            return "object_store.delete"
        return func.attr
    return ""


def _collect_calls(node: ast.AST) -> set[str]:
    """一个函数体里出现的全部被调短名。"""
    calls: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _callee_name(child)
            if name:
                calls.add(name)
    return calls


@dataclass(frozen=True, slots=True)
class FunctionNode:
    module: str
    name: str
    calls: frozenset[str]


def _iter_module_functions(path: Path) -> list[FunctionNode]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[FunctionNode] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found.append(
                FunctionNode(
                    module=path.name, name=node.name, calls=frozenset(_collect_calls(node))
                )
            )
    return found


def _all_functions() -> list[FunctionNode]:
    nodes: list[FunctionNode] = []
    for path in sorted(_CONVERSATION_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        nodes.extend(_iter_module_functions(path))
    return nodes


def _call_graph(nodes: list[FunctionNode]) -> dict[str, set[str]]:
    """函数短名 → 可达的被调短名（同名函数取并集：刻意保守，宁可多报不可漏报）。"""
    graph: dict[str, set[str]] = {}
    for node in nodes:
        graph.setdefault(node.name, set()).update(node.calls)
    return graph


def _closure(name: str, graph: dict[str, set[str]]) -> set[str]:
    """从 ``name`` 出发的可达集合（不含自身）。"""
    seen: set[str] = set()
    stack = list(graph.get(name, set()))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(graph.get(current, set()))
    return seen


def _function_source(func: str, module: str) -> str:
    """入口函数的源码文本（用于"确实触发了 delete_thread Job"这类守卫断言）。"""
    path = Path(module)
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == func:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{module} 里找不到函数 {func}")


# ---------------------------------------------------------------------------
# 1) 登记表自检与"三段齐全"
# ---------------------------------------------------------------------------


def test_registered_entries_exist_and_have_unique_keys() -> None:
    """登记表本身要有效：函数存在、key 唯一。"""
    keys = [entry.key for entry in DELETION_ENTRIES]
    assert len(keys) == len(set(keys)), f"DELETION_ENTRIES 的 key 必须唯一：{keys}"
    cli_keys = [entry.cli_subcommand for entry in DELETION_ENTRIES if entry.cli_subcommand]
    assert len(cli_keys) == len(set(cli_keys)), f"CLI 子命令登记重复：{cli_keys}"
    for entry in DELETION_ENTRIES:
        assert _function_source(entry.func, entry.module), (
            f"{entry.label}：{entry.module} 里找不到 {entry.func}"
        )
        assert entry.tombstone.strip(), f"{entry.label} 缺少 tombstone 语义说明"


def test_every_entry_deletes_objects_hot_segments_and_tombstones() -> None:
    """核心：每个删除入口都必须"删对象 + 删热段 + 落 tombstone（或显式拒绝）"三段齐全。"""
    graph = _call_graph(_all_functions())
    for entry in DELETION_ENTRIES:
        closure = _closure(entry.func, graph)
        assert REQUIRED_OBJECT_DELETION in closure, (
            f"{entry.label} 没有走到对象删除助手 {REQUIRED_OBJECT_DELETION}："
            "删了热段却留下对象镜像（或反之）都属'只删一半'。"
        )
        assert REQUIRED_HOT_DELETION in closure, (
            f"{entry.label} 没有走到热段删除助手 {REQUIRED_HOT_DELETION}："
            "本地热段里的用户原文会留在磁盘上，tombstone 之后再也发现不了。"
        )
        if entry.sweeps_thread:
            assert REQUIRED_THREAD_SWEEP in closure, (
                f"{entry.label} 是 thread 级删除，必须按 <thread_id> 清扫热段目录："
                "object_key IS NULL 的未封存段与 kodo 本地缓存都只能这样删（新发现 6/15）。"
            )
        else:
            assert REQUIRED_THREAD_SWEEP not in closure, (
                f"{entry.label} 是**段级**删除，不得按 thread 清扫目录——"
                "那会连带删掉同 thread 内未过期的段。"
            )
        assert closure & TOMBSTONE_OR_REFUSAL_MARKERS, (
            f"{entry.label} 既不落 tombstone 也不显式拒绝"
            f"（可达标记：{sorted(TOMBSTONE_OR_REFUSAL_MARKERS)}）；"
            "删除失败时保持可重试、成功后才标 deleted 是硬要求（I-6）。"
        )


# ---------------------------------------------------------------------------
# 2) 新入口自动暴露
# ---------------------------------------------------------------------------


def test_no_unregistered_rollout_deletion_path_exists() -> None:
    """扫描全部函数：凡**直接**调用删除原语的函数都必须登记（或说明为何不是入口）。"""
    nodes = _all_functions()
    registered_names = {entry.func for entry in DELETION_ENTRIES}
    trigger_names = {func for _label, _module, func in TRIGGER_ENTRIES}
    unknown: list[str] = []
    for node in nodes:
        if node.name.startswith("__"):
            continue
        touched = node.calls & DIRECT_REMOVAL_MARKERS
        if not touched:
            continue
        if (node.module, node.name) in NON_ENTRY_ALLOWLIST:
            continue
        if node.name in registered_names or node.name in trigger_names:
            continue
        unknown.append(f"{node.module}::{node.name}（直接调用 {sorted(touched)}）")
    assert not unknown, (
        "发现未登记的 rollout 删除路径：\n  "
        + "\n  ".join(sorted(set(unknown)))
        + "\n若它确实会删除 rollout 段/对象或落 thread 墓碑，请加进 DELETION_ENTRIES 并补齐"
        "集成用例（tests/integration/test_rollout_deletion_paths.py）；"
        "若它只是内部助手或能力实现，请加进 NON_ENTRY_ALLOWLIST 并写明原因。"
    )


def test_allowlist_entries_are_not_stale() -> None:
    """白名单不能留死项：原因必须非空，且对应函数仍存在。"""
    names = {(node.module, node.name) for node in _all_functions()}
    for (module, name), reason in NON_ENTRY_ALLOWLIST.items():
        assert reason.strip(), f"{module}::{name} 的白名单原因不能为空"
        assert (module, name) in names, f"白名单 {module}::{name} 已不存在，请删掉该条目"


# ---------------------------------------------------------------------------
# 3) CLI 子命令守卫
# ---------------------------------------------------------------------------


def _cli_mutating_subcommands() -> dict[str, str]:
    """``cli/rollout.py`` 里带 ``--apply`` 的子命令 → 处理函数短名。"""
    path = Path("backend/conversation/cli/rollout.py")
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    parser_vars: dict[str, str] = {}
    mutating_vars: set[str] = set()
    handlers: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "add_parser" and node.args:
            name = node.args[0]
            if isinstance(name, ast.Constant) and isinstance(name.value, str):
                for parent in ast.walk(tree):
                    if isinstance(parent, ast.Assign) and parent.value is node:
                        for target in parent.targets:
                            if isinstance(target, ast.Name):
                                parser_vars[target.id] = name.value
        if isinstance(func, ast.Name) and func.id == "_add_mutation_flags" and node.args:
            argument = node.args[0]
            if isinstance(argument, ast.Name):
                mutating_vars.add(argument.id)
        if isinstance(func, ast.Attribute) and func.attr == "set_defaults":
            handler = None
            for keyword in node.keywords:
                if keyword.arg == "func" and isinstance(keyword.value, ast.Name):
                    handler = keyword.value.id
            if handler is not None and isinstance(func.value, ast.Name):
                handlers[func.value.id] = handler
    result: dict[str, str] = {}
    for var in sorted(mutating_vars):
        name = parser_vars.get(var)
        assert name is not None, f"无法把 {var} 映射到子命令名（CLI 结构变了？）"
        result[name] = handlers.get(var, "")
    return result


def test_cli_mutating_subcommands_are_all_classified() -> None:
    """带 ``--apply`` 的子命令集合必须与登记表精确一致（新子命令必须分类）。"""
    mutating = _cli_mutating_subcommands()
    cli_entries = {
        entry.cli_subcommand: entry for entry in DELETION_ENTRIES if entry.cli_subcommand
    }
    unclassified = {
        name: handler
        for name, handler in mutating.items()
        if name not in CLI_NON_ENTRY_MUTATIONS and name not in cli_entries
    }
    assert not unclassified, (
        f"这些 CLI 子命令带 --apply 但没有分类：{unclassified}。"
        "会删除 rollout 载荷的必须登记进 DELETION_ENTRIES；不会的写进 CLI_NON_ENTRY_MUTATIONS。"
    )
    stale = sorted(set(CLI_NON_ENTRY_MUTATIONS) - set(mutating))
    assert not stale, f"CLI_NON_ENTRY_MUTATIONS 里的子命令已不存在：{stale}"
    assert cli_entries, "至少要登记一个 CLI 删除入口"
    for name, entry in cli_entries.items():
        assert name in mutating, f"登记表里的 {name} 不再是 --apply 子命令（CLI 改名/删除？）"
        assert mutating[name] == entry.func, (
            f"CLI 子命令 {name} 的处理函数已变成 {mutating[name]}，但登记表写的是 {entry.func}"
        )


# ---------------------------------------------------------------------------
# 4) 集成覆盖守卫 + 触发入口
# ---------------------------------------------------------------------------


def test_every_registered_entry_has_an_integration_case() -> None:
    """每个入口都必须在真库真盘参数化用例里出现（防止"登记了但没验证"）。"""
    assert _INTEGRATION_TEST_FILE.is_file(), f"缺少参数化集成用例文件 {_INTEGRATION_TEST_FILE}"
    source = _INTEGRATION_TEST_FILE.read_text(encoding="utf-8")
    missing = [entry.key for entry in DELETION_ENTRIES if entry.key not in source]
    assert not missing, (
        f"这些删除入口在 {_INTEGRATION_TEST_FILE} 里没有对应用例：{missing}。"
        "每个入口都必须在真实文件系统 + 真实 PostgreSQL 上验证"
        "（删除后对象与热文件都不存在、manifest 已 tombstone）。"
    )


def test_trigger_entries_enqueue_the_delete_job_instead_of_deleting_rollouts() -> None:
    """触发型入口只入队 ``delete_thread`` Job，不自己删 rollout 载荷。"""
    nodes = {(node.module, node.name): node for node in _all_functions()}
    for label, module, func in TRIGGER_ENTRIES:
        node = nodes.get((Path(module).name, func))
        assert node is not None, f"找不到触发入口 {module}::{func}"
        touched = node.calls & DIRECT_REMOVAL_MARKERS
        assert not touched, (
            f"{label} 直接调用了删除标记 {sorted(touched)}；"
            "删除必须经由 delete_thread Job（唯一协调器），否则绕过 R4/Outbox/重试纪律。"
        )
        source = _function_source(func, module)
        assert "delete_thread" in source, (
            f"{label} 既没有删载荷也没有触发 delete_thread Job —— 它到底做了什么？"
        )
