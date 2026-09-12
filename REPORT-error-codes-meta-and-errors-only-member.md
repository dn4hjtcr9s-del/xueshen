# 报告：错误码封闭集合元测试 + "errors-only 成员"终态语义（DEV-062 / DEV-072）

- **分支**：`codex/memory-rebuild-implementation`（worktree `.local/worktrees/memory-rebuild-implementation`）
- **范围**：`backend/memory/contracts/errors.py`、`backend/memory/graph/batch.py`、
  `backend/study/contracts/errors.py`（跨域同型漏项，1 行登记）+ 新增/修改的测试
- **未触碰**：`backend/conversation/**`（另一工作流）、`memory-rebuild-deviations.md`（主控登记）、未提交 git
- **用户裁决**：两项都按"**补全同类一致性**"落地 —— 不只修被点名的那一处，对封闭集合补**枚举型元测试**

---

## 0. 结论速览

| # | 事项 | 结论 | 关键落点 |
|---|---|---|---|
| 待办 1① | `BATCH_ALL_MEMBERS_FAILED` 并入 `ERROR_CODES` | ✅ 已登记（连同 2 个同类漏项） | `backend/memory/contracts/errors.py` |
| 待办 1② | 枚举**所有**异常类 code 与集合双向比对 | ✅ 四域全量枚举，再抓出 3 处同类不一致（memory 2 + study 1） | 同上 + `backend/study/contracts/errors.py` |
| 待办 1③ | 枚举型元测试 + 变异演示 | ✅ `tests/unit/test_error_codes_meta.py`（704 行，20 例，7 条规则）；2 次变异实跑变红后撤销 | 新文件 |
| 待办 1④ | 其它同类不一致 | ✅ 全部列出并按"修 or 白名单+原因"处理（见 §1.6） | — |
| 待办 2 | errors-only 成员终态语义 | ✅ 新增 `errors_only` outcome，走**同一条** `release_batch_member` 路径；真值表 + 单元 + 真库集成 | `backend/memory/graph/batch.py`、`tests/integration/test_memory_batch_worker_e2e.py` |
| 附带 | 顺带修掉真值表第 2 行的同类失真 | ✅ "写了但伴随 errors"不再计入"未直接写入"，改为单独警告 | 同上 |

**是否只是文档意义**：`ERROR_CODES` 在本轮之前**没有任何消费者**（`grep -rn "ERROR_CODES" --include=*.py backend tests frontend scripts evals`
只命中 4 处定义 + 本轮新增测试；没有任何模块 `import ERROR_CODES`），登记它**当时纯粹是文档**。
本轮的元测试是它**唯一**的强制约束来源：异常类 code ↔ 集合 ↔ 生产者三方双向绑定，漏登记/死值直接 CI 变红。

---

## 1. 待办 1：错误码封闭集合

### 1.1 登记结果（`backend/memory/contracts/errors.py`）

`ERROR_CODES` 从 36 个 → **39 个**，新增 3 个（都写了"实现期补充"注释说明出处）：

| 新增 code | 生产者（含**真实抛点**，已核对不是死值） | 为什么之前漏了 |
|---|---|---|
| `BATCH_ALL_MEMBERS_FAILED` | `graph/batch.py::BatchAllMembersFailedError`，抛点 `batch.py:589`（DEV-062） | 新加的异常类只写了 `code = BATCH_ALL_MEMBERS_FAILED`（符号引用），集合没跟上 |
| `ACCOUNT_PURGE_IN_PROGRESS` | 同文件 `AccountPurgeInProgressError`，抛点 `api/dependencies.py:233`（§21.3 步骤 1） | 异常类与集合在**同一个文件里**也会漏 |
| `ACCOUNT_PURGE_NOT_DRAINED` | `services/account_purge.py::AccountPurgeNotDrainedError`，抛点 `graph/maintenance.py:287` | 异常类在另一个模块，集合更看不见 |

跨域同型漏项（本报告一并修，仅**加一个集合成员**，无运行时行为变化——该常量在 Study 域无人引用）：

| 域 | 漏项 | 处理 |
|---|---|---|
| study | `StudyRateLimitedError.code = "RATE_LIMITED"`（§17 明确要求该码，`tests/study/test_tasks_sessions_api.py:198` 就在断言它） | ✅ 登记进 `STUDY_ERROR_CODES`（+注释），并由元测试跨域固定 |

### 1.2 双向枚举结果（AST，四域全量）

枚举规则：包内所有"基类名以 `Error` / `Exception` 结尾"的类 → 取其类体里自己声明的 `code`
（字符串字面量，或解析到模块级常量的符号引用；**解析不出来直接失败**，不静默漏项）。

| 域 | 集合大小 | 异常类 | 自带 code 的类 | `code` 位置字面量 | 缺登记 | 死值 |
|---|---|---|---|---|---|---|
| memory | 39（修后） | 53 | 37 | 4 | 0 | 2（`AUTH_REQUIRED` / `AUTH_FORBIDDEN`，白名单+生产者证据） |
| conversation | 25 | 45 | 26 | 1 | 0（`INTERNAL_ERROR` 走共享兜底白名单） | 0 |
| community | 20 | 21 | 21 | 0 | 0（同上） | 0 |
| study | 23（修后） | 25 | 24 | 0 | 0（同上） | 0 |

### 1.3 元测试设计（`tests/unit/test_error_codes_meta.py`，20 例）

| 规则 | 内容 | 为什么需要 |
|---|---|---|
| **R6** | 运行时集合 == 源码 `frozenset({...})` 字面量 | 防止有人改成动态拼装，让整套枚举空转 |
| **R1** | 异常类 `code` ⊆ 集合（四域） | 就是 DEV-062 的形状；失败消息**连类名+行号**一起列出 |
| **R2** | 集合 ⊆ 生产者（异常类 / `code` 位置字面量 / 白名单+生产者证据文件） | "永不抛出的死值"必须显式白名单 + 原因，且**核对生产者文件里真的有那个字面量** |
| **R3** | 没有自己 `code` 的异常类必须登记原因（memory 全域 + 其它三域 `contracts/errors.py`，共 18 条） | 新增异常类要么带 code，要么说明"为什么不带"——封闭集合的第二半 |
| **R4** | `{"code": "X"}` / `code="X"` 形态的字面量 ⊆ 集合（四域） | 绕过异常类直接写公开信封（含 `public_error` 原始 SQL）的路径同样受约束 |
| **R5** | 白名单**双向精确**：每条豁免都要被枚举命中；非异常生产者豁免不得与真实生产者重复 | 防"过期豁免"永久存活（与 `test_degraded_flag_enum_meta.py` 同一纪律） |
| **R7** | `backend/app.py` 产出的公开码必须落在任一域集合或显式登记 | `ERROR_CODES` 不是 memory-api 公开码全集（见 §1.6） |
| 哨兵 | 4 个已知 (类, code) + 1 个已知字面量必须被提取到 | 防元测试自己变成"空转的绿灯" |

白名单（**全部带原因，R5 强制双向**）：

- `NON_CLASS_PRODUCERS`：`AUTH_REQUIRED` / `AUTH_FORBIDDEN` —— 由 `backend/auth/verifier.py`
  的 `AuthError("AUTH_REQUIRED", ...)` 直接构造（元测试核对生产者文件里确有该字面量），
  不走 Memory 异常层级；
- `CODE_POSITION_ALLOWLIST`：`LLM_BUDGET_EXHAUSTED`（图 state 内部码，只进 `state["errors"]`
  参与 dead_letter/needs_review 判定，不进 `PublicError` 信封）、
  `TURN_ATTEMPT_EXHAUSTED`（见 §1.6，待 conversation 域 owner 裁决）；
- `APP_SHELL_ONLY_CODES`：`MAINTENANCE_MODE` / `AUDIT_WRITE_FAILED`（app 外壳产出，验证生产者文件）；
- `EXCEPTION_CODE_ALLOWLIST`：三域基类继承下来的共享信封兜底码 `INTERNAL_ERROR`
  （登记在 Memory 的 `ERROR_CODES`，不算各域"自己"的码）；
- `NO_PUBLIC_CODE_EXCEPTIONS`：18 条"无公开码"异常类的逐条原因（含 `LeaseFencedError`、
  `MaintenanceGateError` 家族、`MemoryClientError`（`code` 来自服务端响应，非类级固定值）等）。

### 1.4 变异演示（实跑 → 变红 → 撤销；md5 已核对一致）

**变异 1：新增一个不在集合里的 code**（在 `services/account_purge.py` 临时插一个
`class MetaDemoUnregisteredError(MemoryError): code = "META_DEMO_UNREGISTERED"`）

```text
E       AssertionError: memory 域有异常类的 code 不在 ERROR_CODES 里：
E           EXCEPTION_CODE_ALLOWLIST 缺少：('memory', 'META_DEMO_UNREGISTERED')
E       assert not ["EXCEPTION_CODE_ALLOWLIST 缺少：('memory', 'META_DEMO_UNREGISTERED')"]
2 failed, 18 passed in 0.63s          # test_exception_codes_are_registered[memory] + test_allowlists_are_exact
-- 撤销后：20 passed in 0.58s
```

（R1 的完整消息里还带类名与行号：`META_DEMO_UNREGISTERED: backend/memory/services/account_purge.py:35
MetaDemoUnregisteredError(code='META_DEMO_UNREGISTERED')`。）

**变异 2：往 `ERROR_CODES` 里塞一个没有生产者的死值**（`"META_DEMO_DEAD_VALUE"`）

```text
E       AssertionError: memory 域的 ERROR_CODES 里有没有任何生产者的死值：['META_DEMO_DEAD_VALUE']
E       （要么补生产者，要么在 NON_CLASS_PRODUCERS 登记原因 + 生产者文件）
1 failed, 19 passed in 0.61s          # test_error_codes_have_producers[memory]
-- 撤销后：20 passed in 0.57s
```

撤销核对：

```text
e66a23572d87a6e8a8590857abb8f6fe  backend/memory/contracts/errors.py
e66a23572d87a6e8a8590857abb8f6fe  /tmp/m_errors.py      （撤销快照）
32720cc4b90cabc130bb187b058d974c  backend/memory/services/account_purge.py
32720cc4b90cabc130bb187b058d974c  /tmp/m_purge.py
```

### 1.5 "登记 ERROR_CODES 是否有实际约束力"的结论

1. **登记之前：没有约束力**，纯粹文档。已核实：`backend/{memory,conversation,community,study}/contracts/errors.py`
   里的 4 个 `*_ERROR_CODES` 常量在 `backend/`、`tests/`、`frontend/`、`scripts/`、`evals/` 里
   **没有任何读取点**（唯一 import 该模块的地方是拿具体异常类，不是拿集合）。
2. **登记之后：由本元测试把它变成 CI 强制约束**。它现在强制三件事：
   （a）异常类新增 `code` → 必须登记；（b）集合新增 code → 必须有生产者，否则白名单+原因；
   （c）任何 `code` 位置的裸字面量 → 必须登记。三者都是**全量枚举**，不是人工清单。
3. **边界（诚实说明）**：它只覆盖"**异常类 + `code` 位置字面量**"这两类生产者。公开码还有第三类
   来源——`app.py` 外壳（见 §1.6），元测试用 `APP_SHELL_ONLY_CODES` 显式登记而不是塞进
   `ERROR_CODES`（那 2 个码不在 §7.3 的错误码目录里，硬塞进去是伪造规格覆盖）。因此
   `ERROR_CODES` 仍然是"Memory 域异常体系的公开码全集"，**不是** memory-api 响应的公开码全集。

### 1.6 发现的其它同类不一致（全部已处理）

| # | 位置 | 性质 | 处理 |
|---|---|---|---|
| 1 | `errors.py::AccountPurgeInProgressError` | 异常 code 不在集合 | ✅ 登记 |
| 2 | `services/account_purge.py::AccountPurgeNotDrainedError` | 异常 code 不在集合（跨模块更容易漏） | ✅ 登记 |
| 3 | `backend/study/contracts/errors.py::StudyRateLimitedError` | 异常 code 不在 `STUDY_ERROR_CODES`（§17 明确要求该码） | ✅ 登记（跨域同型，由元测试固定） |
| 4 | `ERROR_CODES::AUTH_REQUIRED` / `AUTH_FORBIDDEN` | **死值**（无异常类生产者） | ✅ 白名单 + 原因 + 生产者文件证据（`backend/auth/verifier.py`，元测试核对字面量存在） |
| 5 | `backend/conversation/worker/graph_worker.py:260` `"code": "TURN_ATTEMPT_EXHAUSTED"` | 公开信封码不在 `CONVERSATION_ERROR_CODES`（`turn.failed` 事件 payload） | ⚠️ 白名单+原因并**上报**：`backend/conversation/**` 属另一工作流，本轮不代改；建议域 owner 补登记进集合或明确它是"事件码" |
| 6 | `backend/app.py` `MAINTENANCE_MODE` / `AUDIT_WRITE_FAILED` | 公开码不属于任何域集合（app 外壳） | ✅ 登记进 `APP_SHELL_ONLY_CODES` + 生产者证据（R7 也顺带核对 `DATABASE_UNAVAILABLE` / `INTERNAL_ERROR` 属于集合） |
| 7 | `finalize_batch_result` 的 `errors = [] if mutations else state["errors"]` | `state["errors"]` **没有 reducer**，每个成员进入时被重置 → 批次级 `errors` 只反映"最后一条没写入的成员" | ⚠️ 未改（超范围）：影响面仅限 `normalize_result` 的 dead_letter/needs_review 判定；本轮起逐成员错误码已逐条进公开 warnings，可见性不再依赖这个聚合字段。**登记为遗留** |

---

## 2. 待办 2：errors-only 成员的终态语义

### 2.1 真值表（可执行版见 `test_record_batch_member_truth_table`）

| `batch_member_error` | `mutations` | `review_ids` | `errors` | outcome | 进 `batch_failed` | 走 `release_batch_member` | 公开 warnings |
|---|---|---|---|---|---|---|---|
| 有 | 任意 | 任意 | 任意 | `failed` | 是（reason=`member_exception`） | **是** | "处理失败已隔离: reason @node (type: msg)" |
| 无 | ≥1 | 任意 | 任意 | `succeeded` | **否**（它确实写入了） | 否 | 若有 errors：单独一条"已写入 N 条变更…请人工确认完整性" |
| 无 | 0 | ≥1 | 任意 | `needs_review` | 是 | 否（人工结论优先） | "本批 N 条成员未直接写入" |
| 无 | 0 | 0 | **非空** | **`errors_only`（新）** | 是（reason=`member_errors_without_mutation`） | **是**（`attempt_count`+1，达 `max_attempts` 转死信） | "成员 X 处理出错且未写入任何变更…（错误码: …）" |
| 无 | 0 | 0 | 空 | `no_change`（真的无事可做） | 否 | 否 | 无 |

要点：

- **`no_change` vs `errors_only` 的判据就是 `errors` 是否非空**——这是"真的无事可做"与
  "出错导致什么都没写"的唯一分界；
- **`review_ids` 优先于 `errors`**：已有审核候选的成员交人工，不释放（避免同一证据反复产候选）；
- **写入成功的成员永不释放**（哪怕过程中有 errors）：它的 mutation 已落盘，回池子只会重复消耗预算；
- `member_error`（异常隔离）行**优先于一切**：守卫短路时 `commit_result` 必然是空的（`finalize_summary_result`
  还没跑），因此这一行与第 4 行在数据形态上互斥；即便将来出现"写了又抛"，重试也会被
  `_member_already_committed`（按成员 operation 查 `memory_commits`）挡住，不会重复写入。

### 2.2 实现要点（复用**同一条**路径，没有第二套）

1. `MEMBER_OUTCOME_ERRORS_ONLY = "errors_only"` —— 与 `no_change` 分开的新 outcome；
2. `MEMBER_ERRORS_REASON = "member_errors_without_mutation"` —— 写进 `batch_failed[].reason`；
3. `RELEASABLE_MEMBER_REASONS = {member_exception, member_errors_without_mutation}` +
   `is_releasable_failure(entry)` —— **"哪些失败成员回证据池"的唯一判定入口**；
   `_release_failed_members` 由原来的 `entry.get("reason") != MEMBER_FAILURE_REASON` 改为调用它，
   释放循环、`attempt_count` +1、达 `max_attempts` 转死信、`public_error=OPERATION_DEAD_LETTER`
   全部原样复用（无新迁移、无第二套 SQL）；
4. `failed` 列表语义收敛为"**未直接写入的成员**"三选一（needs_review / failed / errors_only）：
   删掉旧的 `or errors`，于是"本批 N 条成员未直接写入"的计数不再把**已写入**的成员算进去
   （旧行为的同类失真，顺带修掉并用警告补足可见性）；
5. `_failure_digest` 支持 errors-only 形态（没有 `failed_node`/`error_type`，改印错误码），
   保证整批全灭时 `public_error.message` 仍有信息量。

### 2.3 与"整批全灭 → `BatchAllMembersFailedError` → dead_letter"的衔接（一致语义）

**释放永远先做，批次级判定在后；两件事互不覆盖。**

1. 先对 `batch_failed` 里 reason 可释放的成员**逐个**走 `release_batch_member`
   （`attempt_count` +1；达 `max_attempts` 则 `dead_letter` 并**保留归属**以便追溯）；
2. 再判定批次：`mutations` 与 `review_candidate_ids` 都空、而 `batch_failed` 非空
   → 抛 `BatchAllMembersFailedError`（`retryable=False` → `classify_failure` → `dead_letter`）；
   否则批次照常 `succeeded`（§5.8 单成员失败不拖垮整批）。

因此：

- **批次 operation 的终态**只由第 2 步决定；
- **成员各自的终态**只由第 1 步决定——整批全灭时成员也已经计过尝试次数（回池或转死信），
  不会像旧实现那样被 `settle_batch_members(status='dead_letter')` 顺手置成 `dead_letter` 而**永久搁浅**
  （旧行为：errors-only 成员在整批全灭时既不释放也被判死，没有任何重试出口）；
- 混合批次里正常成员照常 `succeeded`，errors-only 成员回到 `pending_batch`（`batch_operation_id` 置空），
  下一批重新认领——**在达到 `max_attempts` 之前不会"没写进去却算完成"**。

### 2.4 测试

**单元**（`tests/unit/test_memory_batch_graph.py`，+9 个用例项 = 1 条 6 组参数化 + 3 条，文件 22 passed）：

- `test_record_batch_member_truth_table`（6 组参数）：三维度组合 → outcome / 进 `batch_failed` / 可释放；
- `test_release_failed_members_covers_errors_only_not_review`：真实 `_release_failed_members`
  实现 + fake session，断言只有 `member_exception` 与 `errors_only` 两条被放行（`needs_review`/`no_change` 不释放）；
- `test_finalize_mixed_batch_releases_errors_only_member_and_warns`：批次仍 succeeded、
  "本批 1 条成员未直接写入"、逐条警告含成员 id + 错误码；
- `test_finalize_written_member_with_errors_is_not_counted_unwritten`：写入成功 + errors 不再算未写入，
  改发"请人工确认完整性"。

**真库集成**（`tests/integration/test_memory_batch_worker_e2e.py`，+2 例 / 7 passed，真实 Worker + lease fencing）：

- `test_errors_only_member_is_released_and_visible_in_warnings`（一条 errors-only + 一条正常）：
  批次 `succeeded`；正常成员 `succeeded` 且 `mastery:配方法` 真的写入；errors-only 成员
  `pending_batch` + `batch_operation_id IS NULL` + `attempt_count=1` + 重新出现在
  `list_pending_batch_members`；公开 warnings 里能看出"哪条成员没写成功、错误码是什么"；
- `test_errors_only_member_escalates_to_dead_letter_and_batch_stays_dead_letter`
  （errors-only 成员 `attempt_count = max_attempts - 1`）：
  **整批全灭回归**仍是 `dead_letter` + `public_error.code=BATCH_ALL_MEMBERS_FAILED`
  + 摘要里带 `member_errors_without_mutation(errors: LLM_BUDGET_EXHAUSTED)`；
  成员转 `dead_letter`、`attempt_count=4`、保留归属、`public_error.code=OPERATION_DEAD_LETTER`。

### 2.5 变异演示（实跑 → 变红 → 撤销）

**变异 A：拿掉 `elif errors:` 分支**（等价于 DEV-072 旧行为：errors-only → `no_change`）

```text
FAILED tests/unit/test_memory_batch_graph.py::test_record_batch_member_truth_table[member_error4-...-errors_only-True-True]
1 failed, 21 passed in 0.35s
FAILED tests/integration/test_memory_batch_worker_e2e.py::test_errors_only_member_is_released_and_visible_in_warnings
FAILED tests/integration/test_memory_batch_worker_e2e.py::test_errors_only_member_escalates_to_dead_letter_and_batch_stays_dead_letter
-- 撤销后：22 passed in 0.24s（集成 2 passed）
```

**变异 B：释放白名单回退成只放行 `member_exception`**（errors-only 不再释放）

```text
FAILED tests/unit/test_memory_batch_graph.py::test_record_batch_member_truth_table[...errors_only-True-True]
FAILED tests/unit/test_memory_batch_graph.py::test_release_failed_members_covers_errors_only_not_review
2 failed, 20 passed in 0.28s
FAILED tests/integration/test_memory_batch_worker_e2e.py::test_errors_only_member_is_released_and_visible_in_warnings
FAILED tests/integration/test_memory_batch_worker_e2e.py::test_errors_only_member_escalates_to_dead_letter_and_batch_stays_dead_letter
-- 撤销后：22 passed in 0.22s
```

撤销核对（md5 与快照一致）：`d743177ea6c0476cd3f3052b152116c4  backend/memory/graph/batch.py` == `/tmp/m_batch.py`。

---

## 3. 门禁实跑命令与原始输出尾部

```text
### 1) ruff check backend tests && ruff format --check backend tests && mypy backend
All checks passed!
520 files already formatted
Success: no issues found in 310 source files
退出码=0

### 2) uv run pytest tests/unit tests/test_mineru_ocr_*.py -q
1111 passed, 385 warnings in 17.86s

### 3) uv run pytest tests/contract -q
5 passed, 24 warnings in 2.10s

### 4) DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test" \
      uv run pytest tests/integration/test_memory_batch_worker_e2e.py -q
7 passed, 5 warnings in 1.06s

### 5) 同库串行跑相关批次集成链（memory_batch_graph / batch_cancel_release /
###    evidence_batch_submission / scheduler_pending_batch）
29 passed, 33 warnings in 2.87s
```

（`tests/unit` 含新增的 20 例元测试；`tests/integration/test_memory_batch_worker_e2e.py`
含新增 2 例。真实 PG 相关测试均**串行**执行，未与其他进程并发抢库。）

---

## 4. 改动文件清单

| 文件 | 改动 |
|---|---|
| `backend/memory/contracts/errors.py` | `ERROR_CODES` +3（`BATCH_ALL_MEMBERS_FAILED` / `ACCOUNT_PURGE_IN_PROGRESS` / `ACCOUNT_PURGE_NOT_DRAINED`）+ 出处注释 |
| `backend/memory/graph/batch.py` | `MEMBER_OUTCOME_ERRORS_ONLY` / `MEMBER_ERRORS_REASON` / `RELEASABLE_MEMBER_REASONS` / `is_releasable_failure`；`record_batch_member` 真值表与判定；`finalize_batch_result` 警告与衔接语义；`_failure_digest` 支持 errors-only；`_release_failed_members` 改用唯一判定入口 |
| `backend/study/contracts/errors.py` | `STUDY_ERROR_CODES` +1（`RATE_LIMITED`，跨域同型漏项）|
| `tests/unit/test_error_codes_meta.py`（新增，704 行） | 枚举型元测试：R1–R7 + 白名单双向卫生 + 哨兵，20 例 |
| `tests/unit/test_memory_batch_graph.py` | +errors-only 真值表/释放/可见性 5 组（22 passed） |
| `tests/integration/test_memory_batch_worker_e2e.py` | +真实 PG 混合批次 2 例（errors-only 释放 + 达上限死信/整批全灭回归） |

---

## 5. 遗留与建议（交主控登记）

1. **conversation `TURN_ATTEMPT_EXHAUSTED`**（§1.6 #5）：`turn.failed` 事件里的公开信封码不在
   `CONVERSATION_ERROR_CODES`。建议由该域 owner 决定"补登记进集合"或"明确它是事件码"；
   本轮只在元测试白名单登记原因，不触碰 `backend/conversation/**`。
2. **批次级 `state["errors"]` 只反映最后一条未写入成员**（§1.6 #7）：无 reducer，逐成员重置。
   本轮已让逐成员错误码进公开 warnings，判定语义未变；彻底修需要按成员聚合 errors（属 manager/state 范围）。
3. **`APP_SHELL_ONLY_CODES`**（`MAINTENANCE_MODE` / `AUDIT_WRITE_FAILED`）：它们是 app 外壳的
   公开码、不在 §7.3 目录里。建议在规格 §7.3 补一行"外壳码"或明确它们属独立命名空间。
4. `ERROR_CODES` 的约束边界已在元测试 docstring 与 §1.5 写明：**异常体系 + `code` 字面量**，
   不含 app 外壳与其它域的公开码。
