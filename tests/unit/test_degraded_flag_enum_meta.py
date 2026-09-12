"""降级标记的**封闭枚举**元测试（review I-10 / review-2 新发现 2）。

**为什么必须是元测试**：`DegradedFlag` 是封闭 ``Literal``，而消费它的
``TurnDegradedPayload.flags`` / ``AnswerCompletedPayload.degraded_flags`` 都是
``extra="forbid"``。生产端只要写了一个 Literal 里没有的标记，``finalize`` 就会在**事务内**
的 ``validate_event_payload`` 抛 ``ValidationError`` 且无 try/except → **整个事务回滚、
已经流出的回答丢失**。这个缺陷已经漏过两次：

- I-10：``answer_stream_interrupted/truncated/refused`` 三个标记没进 Literal；
- review-2 新发现 2：``rewrite_structured_fallback`` / ``rewrite_plan_contract_invalid`` /
  ``evidence_structured_fallback`` 三个标记同样没进。

逐点补测试治不了"下次再漏"，所以这里改成**全量枚举**：AST 遍历
``backend/conversation/**`` 提取所有被写进降级标记集合的字符串字面量，与
``get_args(DegradedFlag)`` 做集合比较。新增生产者字面量而忘记补 Literal → 本文件变红
（报告里有一次"临时加一个字面量 → 变红 → 撤销"的演示）。

四组断言：

1. **正向（硬）**：进 state/SSE 的字面量集合 ⊆ ``DegradedFlag``（缺项直接列在断言消息里）；
2. **反向（硬）**：Literal 里没有"永不产生"的死值；确有死值时必须显式白名单 + 原因，
   且白名单是**双向精确**的（将来补上生产者后必须删掉白名单项，否则失败）；
3. **旁路（硬）**：只进 rollout 审计记录（``contracts/rollout.py`` 的
   ``degraded_flags: list[str]``，不是 SSE 契约）的字面量同样要显式登记；
4. **形状守卫**：写降级标记的站点形状必须被本元测试认识；出现新形状（例如列表里混了
   f-string）时失败，迫使作者回来更新提取器——防止元测试自己变成"空转的绿灯"。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import get_args

from backend.conversation.contracts.api import DegradedFlag

#: 生产端所在的包根（相对仓库根）。
_CONVERSATION_ROOT = Path("backend/conversation")

#: 标记写入的"去向"。
#: - ``state``：进 ``state["degraded_flags"]`` 或 ``turn.degraded`` 事件 payload，
#:   最终被 ``AnswerCompletedPayload`` / ``TurnDegradedPayload`` 的封闭 Literal 校验；
#: - ``rollout``：只进 rollout 审计记录（``record_rollout`` 的 payload），
#:   该字段在 ``contracts/rollout.py`` 里是开放的 ``list[str]``。
SINK_STATE = "state"
SINK_ROLLOUT = "rollout"

#: 承载降级标记的 kwarg / dict key 名。
_FLAG_KEYS = frozenset({"degraded_flags", "flags"})

#: 逐条 append/extend 的接收者名（只认 ``degraded_flags``：``flags`` 在别处是 feature flag 字典）。
_FLAG_LIST_NAMES = frozenset({"degraded_flags"})

#: ``DegradedFlag`` 里**当前没有生产者**的取值：白名单必须精确匹配，
#: 补上生产者后要删掉对应项（否则"死值"会永久留在契约里而无人发现）。
NEVER_PRODUCED: dict[str, str] = {
    "retrieval_partial": (
        "冻结契约 §17.4.1 为检索链路预留的取值；当前检索节点不产出任何降级标记，"
        "因此这是**已知死值**（review-2 深挖新发现，已在元测试显式登记）。"
    ),
    "retrieval_unavailable": (
        "同上：冻结契约预留、当前无生产者。"
        "tests/unit/test_rollout_node_wiring.py 里的同名串只是测试夹具，不算生产者。"
    ),
}

#: 只进 rollout 审计记录、**有意不进** SSE 封闭 Literal 的标记（精确匹配）。
ROLLOUT_ONLY: dict[str, str] = {
    "graph_failed": (
        "图在 finalize 之前失败时 runner 补写的 turn_completed 记录标记；"
        "它只落 rollout 段（contracts/rollout.py 的 degraded_flags 是开放的 list[str]），"
        "不经过 answer.completed / turn.degraded 的封闭 Literal，因此不进 DegradedFlag。"
    ),
}

#: 形状守卫：被识别为"标记写入"但值不是"纯字符串列表字面量"的站点，
#: 键是 ``(模块名, ast.unparse(值))``。每一项都要写清"为什么它仍然是安全的"。
#: 目前为空——所有站点都是纯字面量或直通传递。
KNOWN_NON_FLAG_SINKS: dict[tuple[str, str], str] = {}


@dataclass(frozen=True, slots=True)
class FlagWrite:
    """一处降级标记字面量写入。"""

    module: str
    line: int
    value: str
    sink: str

    def __str__(self) -> str:
        return f"{self.module}:{self.line} [{self.sink}] {self.value!r}"


@dataclass(frozen=True, slots=True)
class UnhandledSink:
    """一处形状无法静态判定的标记写入（形状守卫用）。"""

    module: str
    line: int
    rendered: str

    def __str__(self) -> str:
        return f"{self.module}:{self.line} {self.rendered}"


def _is_sequence_literal(node: ast.AST) -> bool:
    return isinstance(node, ast.List | ast.Tuple | ast.Set)


class _FlagWriteCollector(ast.NodeVisitor):
    """收集一个模块里所有降级标记写入。"""

    def __init__(self, module: str, rollout_payload_ids: set[int]) -> None:
        self.module = module
        self.writes: list[FlagWrite] = []
        self.unhandled: list[UnhandledSink] = []
        self._rollout_payload_ids = rollout_payload_ids
        self._sink_stack: list[str] = [SINK_STATE]
        self._handled: set[int] = set()

    # -- 工具 --------------------------------------------------------------

    def _record(self, node: ast.AST, value: str, *, sink: str | None = None) -> None:
        self.writes.append(
            FlagWrite(
                module=self.module,
                line=getattr(node, "lineno", 0),
                value=value,
                sink=sink or self._sink_stack[-1],
            )
        )

    def _record_unhandled(self, node: ast.AST) -> None:
        self.unhandled.append(
            UnhandledSink(
                module=self.module,
                line=getattr(node, "lineno", 0),
                rendered=ast.unparse(node),
            )
        )

    def _handle_flag_value(self, node: ast.AST, *, sink: str) -> None:
        """处理一个"降级标记集合"（或单条标记）的值。

        规则刻意保守：**只解析字符串字面量**；``Name``/``Attribute``/``Subscript``
        都视为"直通或外部构造"（例如 ``degraded_flags=[flag]``、``[str(f) for f in ...]``），
        不产生新字面量。序列字面量里出现 f-string / 函数调用等**构造型**元素则记为
        未识别形状 → 变红，防止元测试自己变成"空转的绿灯"。
        """
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            self._record(node, node.value, sink=sink)
            return
        if not _is_sequence_literal(node):
            return
        for element in node.elts:
            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                self._record(element, element.value, sink=sink)
            elif isinstance(element, ast.Name | ast.Attribute | ast.Subscript):
                continue
            else:
                self._record_unhandled(node)
                return

    # -- 访问器 ------------------------------------------------------------

    def visit_Call(self, node: ast.Call) -> None:
        name = _callee_name(node)
        if name == "_emit_degraded":
            for argument in node.args[2:]:
                self._handle_emitted_flag(argument)
            for keyword in node.keywords:
                if keyword.arg == "flag":
                    self._handle_emitted_flag(keyword.value)
            return
        if (
            isinstance(node.func, ast.Attribute)
            and node.func.attr in {"append", "extend", "add"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in _FLAG_LIST_NAMES
        ):
            for argument in node.args:
                self._handle_flag_value(argument, sink=self._sink_stack[-1])
            return
        for keyword in node.keywords:
            if keyword.arg in _FLAG_KEYS and id(keyword.value) not in self._handled:
                self._handled.add(id(keyword.value))
                self._handle_flag_value(keyword.value, sink=self._sink_stack[-1])
        self.generic_visit(node)

    def _handle_emitted_flag(self, node: ast.AST) -> None:
        """``_emit_degraded(runtime, state, X)`` 的 X：字面量 → 收集；f-string → 形状守卫。"""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            self._record(node, node.value, sink=SINK_STATE)
        elif isinstance(node, ast.JoinedStr):
            self._record_unhandled(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._handle_assignment_target(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._handle_assignment_target(node.target, node.value)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._handle_assignment_target(node.target, node.value)
        self.generic_visit(node)

    def _handle_assignment_target(self, target: ast.AST, value: ast.AST) -> None:
        if id(value) in self._handled:
            return
        is_flag_target = (isinstance(target, ast.Name) and target.id in _FLAG_LIST_NAMES) or (
            isinstance(target, ast.Subscript)
            and isinstance(target.slice, ast.Constant)
            and target.slice.value in _FLAG_KEYS
        )
        if is_flag_target:
            self._handled.add(id(value))
            self._handle_flag_value(value, sink=self._sink_stack[-1])

    def visit_Dict(self, node: ast.Dict) -> None:
        sink = SINK_ROLLOUT if id(node) in self._rollout_payload_ids else self._sink_stack[-1]
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value in _FLAG_KEYS
                and id(value) not in self._handled
            ):
                self._handled.add(id(value))
                self._handle_flag_value(value, sink=sink)
        self.generic_visit(node)


def _callee_name(node: ast.Call) -> str:
    """被调函数的短名（``a.b.c`` → ``c``）。"""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _rollout_payload_dict_ids(tree: ast.AST) -> set[int]:
    """``record_rollout(runtime, type, {...})`` 的 payload 字典及其子节点 id。

    这些字典里的 ``degraded_flags`` 走 rollout 审计记录（开放的 ``list[str]``），
    与 SSE 的封闭 Literal 是两条不同的去向，必须分开登记。
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _callee_name(node) != "record_rollout":
            continue
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            if isinstance(argument, ast.Dict):
                ids.update(id(child) for child in ast.walk(argument))
    return ids


def _iter_modules() -> list[Path]:
    return sorted(
        path for path in _CONVERSATION_ROOT.rglob("*.py") if "__pycache__" not in path.parts
    )


def _collect_all() -> tuple[list[FlagWrite], list[UnhandledSink]]:
    """遍历 ``backend/conversation/**``，返回 (标记写入, 未识别形状)。"""
    writes: list[FlagWrite] = []
    unhandled: list[UnhandledSink] = []
    for path in _iter_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _FlagWriteCollector(path.name, _rollout_payload_dict_ids(tree))
        collector.visit(tree)
        writes.extend(collector.writes)
        unhandled.extend(collector.unhandled)
    return writes, unhandled


def _literal_of(annotation: object) -> set[str]:
    """``list[DegradedFlag]`` → DegradedFlag 的取值集合。"""
    args = get_args(annotation)
    return set(get_args(args[0])) if args else set()


# ---------------------------------------------------------------------------
# 提取器自检
# ---------------------------------------------------------------------------


def test_extractor_finds_known_producers() -> None:
    """提取器自检：已知生产站点必须被提取到（否则下面的集合断言是"空转的绿灯"）。"""
    writes, _unhandled = _collect_all()
    found = {(write.module, write.value) for write in writes}
    expected = {
        ("memory.py", "memory_unavailable"),
        ("memory.py", "memory_degraded"),
        ("memory.py", "memory_prime_degraded"),
        ("memory.py", "memory_prime_unavailable"),
        ("answer.py", "answer_stream_interrupted"),
        ("answer.py", "answer_stream_truncated"),
        ("answer.py", "answer_stream_refused"),
        ("answer.py", "citation_degraded"),
        ("memory_tool.py", "memory_tool_degraded"),
        ("memory_tool.py", "memory_tool_truncated"),
        ("memory_tool.py", "memory_tool_budget_exceeded"),
        ("rewrite.py", "rewrite_structured_fallback"),
        ("rewrite.py", "rewrite_plan_contract_invalid"),
        ("evidence.py", "evidence_structured_fallback"),
    }
    missing = sorted(expected - found)
    assert not missing, f"提取器漏掉了已知生产站点（元测试本身失效）：{missing}"
    assert {write.sink for write in writes} == {SINK_STATE, SINK_ROLLOUT}, (
        "两种去向都应当被提取到：state（SSE 封闭 Literal）与 rollout（审计记录）"
    )


# ---------------------------------------------------------------------------
# 1) 正向：进 state/SSE 的字面量必须都在 Literal 里
# ---------------------------------------------------------------------------


def test_state_sink_literals_are_inside_degraded_flag_literal() -> None:
    """新增降级标记而忘记补 Literal → 本断言变红（缺项直接列出来）。"""
    writes, _unhandled = _collect_all()
    allowed = set(get_args(DegradedFlag))
    produced = {write.value for write in writes if write.sink == SINK_STATE}
    missing = sorted(produced - allowed)
    assert not missing, (
        f"以下降级标记会进 state/SSE，但不在 DegradedFlag（封闭 Literal）里：{missing}。"
        "请在 backend/conversation/contracts/api.py 的 DegradedFlag 补齐——否则 finalize "
        "事务内的 validate_event_payload 会抛错并回滚整个事务。"
        f"相关站点：{sorted(str(w) for w in writes if w.value in missing)}"
    )


def test_rollout_only_literals_are_registered() -> None:
    """旁路：只进 rollout 审计记录的标记同样要显式登记（精确匹配，不允许悄悄新增）。"""
    writes, _unhandled = _collect_all()
    allowed = set(get_args(DegradedFlag))
    produced = {write.value for write in writes if write.sink == SINK_ROLLOUT}
    unregistered = sorted(produced - allowed - set(ROLLOUT_ONLY))
    assert not unregistered, (
        f"以下标记只出现在 rollout 审计记录里且未登记：{unregistered}。"
        "若它确实不进 SSE（不经过 answer.completed / turn.degraded 的封闭 Literal），"
        "请加进本文件的 ROLLOUT_ONLY 并写明原因；否则请补进 DegradedFlag。"
    )
    stale = sorted(set(ROLLOUT_ONLY) - produced)
    assert not stale, f"ROLLOUT_ONLY 里的这些标记已无生产者，请删掉白名单项：{stale}"


# ---------------------------------------------------------------------------
# 2) 反向：Literal 里不允许有死值
# ---------------------------------------------------------------------------


def test_no_dead_values_in_degraded_flag_literal() -> None:
    """反查死枚举：死值必须显式白名单，且白名单双向精确。"""
    writes, _unhandled = _collect_all()
    produced = {write.value for write in writes}
    dead = sorted(set(get_args(DegradedFlag)) - produced)
    unregistered = sorted(set(dead) - set(NEVER_PRODUCED))
    assert not unregistered, (
        f"DegradedFlag 里这些取值没有任何生产者（死枚举）：{unregistered}。"
        "请删除该取值，或在 NEVER_PRODUCED 里登记原因。"
    )
    stale = sorted(set(NEVER_PRODUCED) - set(dead))
    assert not stale, f"NEVER_PRODUCED 里的这些取值已有生产者，请删掉白名单项：{stale}"


# ---------------------------------------------------------------------------
# 3) 形状守卫：新写入形状必须回来更新提取器
# ---------------------------------------------------------------------------


def test_flag_write_shapes_are_recognized() -> None:
    """出现提取器不认识的写入形状（例如列表里混 f-string）时变红。"""
    _writes, unhandled = _collect_all()
    unknown = sorted(
        {
            (sink.module, sink.rendered)
            for sink in unhandled
            if (sink.module, sink.rendered) not in KNOWN_NON_FLAG_SINKS
        }
    )
    assert not unknown, (
        f"出现本元测试无法静态判定的降级标记写入形状：{unknown}。"
        "若是直通传递（原样转发既有集合），请写成 Name/Attribute/推导式；"
        "若确实不是降级标记，请加进 KNOWN_NON_FLAG_SINKS 并写明原因；"
        "否则请改写成字符串列表字面量，让本测试能固定它。"
    )


# ---------------------------------------------------------------------------
# 4) 契约自检与端到端复现
# ---------------------------------------------------------------------------


def test_literal_is_shared_by_all_sse_consumers() -> None:
    """Literal 的取值必须被 SSE payload 原样消费（防止 Literal 被复制成两份）。"""
    from backend.conversation.contracts.api import AnswerCompletedPayload, TurnDegradedPayload

    allowed = set(get_args(DegradedFlag))
    answer_flags = _literal_of(AnswerCompletedPayload.model_fields["degraded_flags"].annotation)
    degraded_flags = _literal_of(TurnDegradedPayload.model_fields["flags"].annotation)
    assert answer_flags == allowed, (
        "AnswerCompletedPayload.degraded_flags 的 Literal 与 DegradedFlag 不一致"
    )
    assert degraded_flags == allowed, "TurnDegradedPayload.flags 的 Literal 与 DegradedFlag 不一致"


def test_every_produced_flag_validates_against_answer_completed_payload() -> None:
    """端到端（无 DB）：每个 state 标记都能通过 answer.completed 契约校验。

    这是"finalize 事务回滚"那条后果的直接复现，比集合断言更贴近真实故障路径。
    """
    from uuid import uuid4

    from backend.conversation.contracts.api import AnswerCompletedPayload

    writes, _unhandled = _collect_all()
    produced = sorted({write.value for write in writes if write.sink == SINK_STATE})
    assert produced, "没有提取到任何 state 降级标记，元测试失效"
    for index, flag in enumerate(produced):
        payload = AnswerCompletedPayload(
            assistant_message_id=uuid4(),
            thread_version=index,
            answer="ok",
            degraded_flags=[flag],
        )
        assert payload.degraded_flags == [flag]
