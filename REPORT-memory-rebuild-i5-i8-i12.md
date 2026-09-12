# 修复报告：I-5 / I-8 / I-12 + 指标 + 调度（worktree `memory-rebuild-implementation`）

分支：`codex/memory-rebuild-implementation`；范围：review §4 的 **I-5 / I-8 / I-12**、
§5「指标 / 可观测性」「调度 / 批处理」各一项、§7 第 9 项中属于本 agent 的部分。
**未提交 git。**

---

## 0. 契约新增字段（按要求明确写出）

| 字段 | 位置 | 类型 | 默认值 | 语义 |
|---|---|---|---|---|
| `index_entries_truncated` | `backend/memory/contracts/results.py::MemoryToolPrimeResponse` | `bool` | `False` | `true` = `index_entries` 只是按 `memory_id` 升序的**前缀**、不是全量注册表；需要更多主题时走 `memory.search` |

- 追加在模型**末尾**：既有字段的名字、声明顺序、语义都不变（`MemoryToolPrimeResponse` 的
  其它字段一个没删没改），旧调用方不传该字段仍可构造。
- 服务端条数上限常量：`backend/memory/services/memory_tools.py::PRIME_INDEX_ENTRIES_MAX = 200`
  （防御性上限，不是产品配额；注入提示词的最终体积另由 conversation 侧 token 预算兜底）。
- 同时 `degraded` 在**仅目录被条数上限截断**时也置 `true`：服务端「内容不完整」的信号
  保持单一入口，节点侧据此发 `memory_prime_degraded`。
- `tests/contract/openapi_snapshot.json` 已按 AGENTS.md 的流程重新生成
  （`UPDATE_OPENAPI_SNAPSHOT=1 .venv/bin/python -m pytest tests/contract -q`），
  该次再生成的 diff **只有我这 5 行**（其余 agent 的改动当时未改变 OpenAPI 面）。

---

## 1. I-5 prime 注入无界（Important）

### 修法（两侧都加界）

**服务端（有界返回 + 可判定信号）**

- `fetch_index_projection(..., limit: int | None = None)`：`limit` 下推 SQL `LIMIT :limit`
  （`None` = 既有语义不变），`prime()` 传 `PRIME_INDEX_ENTRIES_MAX + 1` 判定超限——
  **多取一条即可判定，不把整份注册表读进内存**；超限时取前 200 条并置
  `index_entries_truncated=true`（同时 `degraded=true`），日志
  `memory_prime_index_truncated: kept=200`（不含 user_id）。
- 保留既有字段与顺序：只追加一个可选字段。

**节点侧（按预算裁剪 + 可观测降级标记）**

- 新增 `backend/conversation/services/context_service.py::trim_prime_to_budget()`：
  逐条按 JSON 序列化后的 token 计入，保留**前缀**（服务端固定序），一旦裁剪即置
  `index_entries_truncated=true`。计数口径与 `memory_tool._apply_token_budget`、
  `_apply_token_cap` 一致（`budget<=0` 或没有 token 计数器时不裁剪）。
- **预算选择：复用 `conversation_memory_token_budget`，不新增 setting。**
  理由：prime 与旧 memory 读取、memory 工具结果是**同一类「长期记忆进提示词」的内容**，
  三者共用一份预算才能让快照申报的 `budgets.memory_tokens` 与实际注入量一致；另开一个
  setting 会让「旧读取 + tool 结果 + prime」之和超出快照声明的预算。
- `graph/nodes/memory.py::_apply_prime_budget()` 在注入前裁剪；发生裁剪时
  `runtime.logger.warning("memory_prime_index_truncated: kept=%d budget_tokens=%d")`
  并走**既有** `_emit_degraded(..., "memory_prime_degraded")`——flag 已在
  `DegradedFlag` Literal 中，无需改对话契约。
- 快照 status/truncated 语义收紧为「summary 或目录被截断」（`_is_prime_truncated`）；
  `_pin_prime` 的 `truncated` 同样含目录裁剪，`index_entry_count` 记的是**实际注入**的条数。
- 兜底两层：`ContextService._memory_from_context`（快照唯一构造点）与
  `_long_term_memory_view`（视图出口）各按同一预算再裁一次，后者专门兜住**修复前写入的
  checkpoint**（`snapshot_from_dict` 会原样还原未裁剪的 prime）；已裁剪的 prime 走到
  这两处是 no-op。

### 测试

- `tests/unit/test_memory_tools.py`：新增
  `test_prime_index_entries_are_bounded_with_decidable_signal`（260 条 → 200 条、
  前缀确定、`limits == [201]`、`degraded=true`）与
  `test_prime_index_entries_at_cap_is_not_marked_truncated`（边界正例，防止"永远报截断"）；
  假 `fetch_index_projection` 同步支持 `limit`；端点的 `set(body)` 断言补新字段。
- `tests/unit/test_prime_injection_budget.py`（新增，8 项）：`trim_prime_to_budget`
  的前缀/标记/不原地修改/预算关闭语义；节点侧「注入有界 + 实际注入 token ≤ 预算 +
  发 `memory_prime_degraded`（经 `TurnDegradedPayload` 封闭 Literal 校验）+ pin 记录实际条数」；
  预算内**不得**产生假降级；服务端已截断时节点照样发标记；快照不变量与"旧 checkpoint
  视图出口仍要有界"。
- `tests/integration/test_memory_tools_sql.py`：
  `test_projection_limit_is_bounded_deterministic_prefix`（真实 PG：`LIMIT` 生效且是
  全序前缀，多取一条即可判定 truncated）。

### 命令与输出尾部

```
$ uv run pytest tests/unit/test_memory_tools.py tests/unit/test_prime_injection_budget.py -q
37 passed        # test_memory_tools.py
8 passed         # test_prime_injection_budget.py
$ DATABASE_URL=postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test \
  uv run pytest tests/integration/test_memory_tools_sql.py -q
10 passed, 5 warnings in 0.78s
```

---

## 2. I-8 取消批次不释放成员 / 被取消的证据仍被处理（Important）

### 修法

- `persistence/operations.py::request_cancel`：在**立即取消**的三条分支
  （`queued` / `retry_wait` / `pending_batch` / `needs_review`）之后，同一事务里调用
  **既有** `settle_batch_members(batch_operation_id=operation_id, status="cancelled")`
  ——直接复用该函数的 `cancelled` 语义（成员状态仍 `pending_batch`、`batch_operation_id`
  置 NULL），**没有写第二套释放逻辑**；非批次 operation 恒为 0 行。
  `running` 分支**故意不释放**：批次还在跑、成员仍归它，终态取消由
  `complete_operation(status='cancelled')` 走同一套 settle。
- `list_batch_member_operations`：加 `AND status = 'pending_batch'`，只返回**可写**成员。
  用户取消的成员行状态是 `cancelled` 但归属字段仍在，不过滤就会被批次照常捞出来处理。
- `begin_batch_member` 的防御落在**它唯一的数据来源**（上面的仓储函数）：
  返回给调用方的成员一定是 `pending_batch`。
  ⚠️ `backend/memory/graph/batch.py` 属于另一个 agent（明确在"不要改"清单里），
  因此**没有**在该函数内再加一层判断；如主控要求函数内二次防御，请转给 batch.py 的
  owner（我的过滤已保证其入参可写）。

### 连带效果（review 里提到的两个后果都消除）

- 取消批次后成员回到池子 → `list_pending_batch_user_ids` / `list_pending_batch_members`
  重新看得到它们 → `_sweep_batch_runs` 的"该用户已无待入批证据"不成立，
  **run 不会再被收尾成 succeeded 掩盖问题**，下一个 0 点正常入批。
- `account_purge` 的取消循环不受影响：它先物化整份行快照再逐条 `request_cancel`，
  批次释放出来的成员仍在快照里、会被逐条取消（8 项 purge 集成测试全绿）。

### 测试

- `tests/integration/test_batch_cancel_release.py`（新增，6 项，真实 PG）：
  1. `request_cancel`（API/管理路径，**不是**直接调 settle）取消批次 → 成员状态
     `pending_batch`、归属 NULL、重新出现在用户/成员扫描里、**能被下一个批次正常领走**；
  2. 普通 operation 取消不受影响（没有成员可释放）；
  3. 取消单条证据 → 仓储层过滤后批次看不到它；用假 LLM 队列证明"若处理它就会失败"：
     批次仍 `succeeded`、`memory_commits` 只有 1 条、只有存活那条落文档、
     被取消证据的主题**没有**任何 mastery 文档；再走真实 `claim → complete(succeeded)`，
     存活成员转 `succeeded`、**被取消的仍是 `cancelled`**（批次收尾不会"复活"它）；
  4. `list_open_runs` 的两条 SQL 语义（见 §4）。

### 命令与输出尾部

```
$ DATABASE_URL=postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test \
  uv run pytest tests/integration/test_batch_cancel_release.py -q
6 passed, 5 warnings in 0.75s     # 连续 4 次运行均全绿
$ ... uv run pytest tests/integration/test_memory_batch_graph.py \
      tests/integration/test_account_purge.py tests/integration/test_worker_processes.py -q
20 passed, 6 warnings in 2.96s
```

---

## 3. I-12 前端镜像漏 `frontmatter_patch`（Important）

`frontend/src/api/memory.ts::MutationResult.action` 补齐 `"frontmatter_patch"`，与
`backend/memory/contracts/operations.py::MutationResult.action` 一致（放在
`append_evidence` 之后，保持与后端声明顺序一致），并加一行中文注释说明镜像来源。

```
$ cd frontend && npm run build      # tsc --noEmit && vite build
✓ built in 238ms                    # exit 0（仅 chunk>500kB 的既有提示）
$ cd frontend && npm run test
Test Files  12 passed (12)
     Tests  94 passed (94)          # exit 0
```

---

## 4. 指标：`memory_operations_total{status}` 在批量路径失真（Minor）

`backend/memory/api/dependencies.py::submit_operation` 的发射点由无条件
`status="queued"` 改为 `status=evidence_status`（`_evidence_batch_gate` 的返回值：
批量门控开启时证据实际以 `pending_batch` 插入，关闭时仍是 `queued`），并加注释说明口径。
说明：review 里写的 `210/262` 在当前 revision 只有**一处**发射点（262 行），另一处已不存在。

测试：`tests/integration/test_evidence_batch_submission.py::
test_operation_metric_label_follows_the_actual_inserted_status`（真实 PG + 真实 API，读
Counter 增量：开批量 → `pending_batch` +1、`queued` 不变；关批量 → `queued` +1）。

```
$ DATABASE_URL=... uv run pytest tests/integration/test_evidence_batch_submission.py -q
8 passed, 33 warnings in 1.71s
```

---

## 5. 调度：`list_open_runs` 分页陷阱（Minor）

### 选择：**过滤下推 SQL**（而不是 DESC 取最近 N 条）

`backend/memory/persistence/maintenance.py::list_open_runs` 新增可选参数
`idempotency_suffix: str | None = None` → `AND idempotency_key LIKE '%' || :suffix`
（`None` = 既有语义，向后兼容）；`ORDER BY created_at ASC LIMIT :limit` 保持不变。
调用方 `worker/scheduler.py` 传模板现算的后缀 `_batch_run_key_suffix(date)`（形如
`":2026-09-12"`，由 `BATCH_IDEMPOTENCY_KEY_TEMPLATE` 推导，与
`_batch_run_user_id` 共用同一处推导，避免"一边按新模板建 run、一边按旧模板筛"）；
Python 侧的 `endswith(suffix)` 保留为**保守兜底**（宁可不收尾也不误关别的日期）。

**为什么选下推而不是 `created_at DESC`：**
1. 语义精确：不管库里堆了多少历史 running run，返回的候选**只可能是当天**的 run，
   历史残留占用 `limit` 名额这件事被彻底消除；DESC 只是让"最近的 limit 条"优先，
   当天 run 数本身超过 limit 时仍要靠后续 tick 慢慢推，且语义依赖"created_at 与本地
   日期单调"，多一层隐含假设。
2. DESC 会改变返回顺序与"最早优先"的既有语义（虽然当前 sweep 不依赖顺序），
   下推保持了 `created_at ASC` 与调用方的既有行为，改动面更小、更易回归验证。

### 测试

- 单测 `tests/unit/test_scheduler_pending_batch.py::TestSweepPagination::
  test_stale_runs_do_not_crowd_out_todays_run`：10 条更早入账的历史残留 + `limit=1`，
  断言仓储收到的后缀实参是 `":2026-08-11"`、当天 run 被收尾、残留一个都没被误关；
  假仓储同步"先过滤再截断"的 SQL 语义（旧实现会在这一步失败）。
  假 `list_open_runs` 同步新签名（新增 `list_open_run_suffixes` 记录）。
- 集成 `tests/integration/test_batch_cancel_release.py::test_list_open_runs_suffix_filter_is_pushed_down`
  （真实 PG：30 条历史残留 + `limit=1`，过滤后必须拿到当天 run；不过滤只会拿到
  `2026-07-01` 的残留——正是被修复的挤占）与
  `...::test_list_open_runs_without_suffix_keeps_legacy_semantics`（不传后缀的既有调用方
  仍按 `created_at ASC` 返回全部 running run）。

---

## 6. 改动文件清单

**代码（我名下）**

| 文件 | 改动 |
|---|---|
| `backend/memory/services/memory_tools.py` | `PRIME_INDEX_ENTRIES_MAX=200`；`fetch_index_projection(limit=)` 下推 SQL；`prime()` 有界返回 + 截断判定/日志/双信号 |
| `backend/memory/contracts/results.py` | `MemoryToolPrimeResponse` 追加可选字段 `index_entries_truncated: bool = False` |
| `backend/conversation/services/context_service.py` | 新增 `trim_prime_to_budget()`；快照构造与 answer 视图出口按同一预算裁剪并置标记 |
| `backend/conversation/graph/nodes/memory.py` | `_apply_prime_budget()` / `_is_prime_truncated()`；注入前裁剪 + `memory_prime_degraded`；status/truncated/pin 语义含目录截断；模块 docstring 补"注入有界" |
| `backend/memory/persistence/operations.py` | `request_cancel` 立即取消时同事务释放成员（复用 `settle_batch_members`）；`list_batch_member_operations` 加 `status='pending_batch'` 过滤 |
| `backend/memory/api/dependencies.py` | `memory_operations_total` 按实际插入状态打标签 |
| `frontend/src/api/memory.ts` | `MutationResult.action` 补 `"frontmatter_patch"` |
| `backend/memory/persistence/maintenance.py` ⚠️ | `list_open_runs` 新增 `idempotency_suffix` 过滤下推（**该文件不在我最初的文件清单里**，但调度项只能落在它上面；不在任何"不要改"清单中） |
| `backend/memory/worker/scheduler.py` ⚠️ | `_batch_run_key_suffix()`；sweep 传后缀 + docstring 更新（同上说明） |

**测试**

- 新增：`tests/unit/test_prime_injection_budget.py`、`tests/integration/test_batch_cancel_release.py`
- 修改：`tests/unit/test_memory_tools.py`、`tests/unit/test_scheduler_pending_batch.py`、
  `tests/integration/test_memory_tools_sql.py`、`tests/integration/test_evidence_batch_submission.py`
- 生成物：`tests/contract/openapi_snapshot.json`（+5 行，只有我的新字段）

**未改**（按要求）：`memory_service.py`、`graph/batch.py`、`graph/summary.py`、
`conversation/worker/**`、`rollout/**`、`services/thread_deletion.py`、`app.py`、
`graph/nodes/memory_tool.py`、`graph/runner.py`、`graph/consolidation.py`、
`contracts/api.py`、`memory-rebuild-deviations.md`。

---

## 7. 门禁结果（全绿）

```
$ uv run ruff check backend tests
All checks passed!
$ uv run ruff format --check backend tests
514 files already formatted
$ uv run mypy backend
Success: no issues found in 309 source files
$ uv run pytest tests/unit tests/test_mineru_ocr_*.py -q
1015 passed, 385 warnings in 17.69s
$ DATABASE_URL=postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test \
  uv run pytest tests/integration/test_batch_cancel_release.py \
    tests/integration/test_memory_tools_sql.py \
    tests/integration/test_evidence_batch_submission.py -q
24 passed, 33 warnings in 2.44s
$ uv run pytest tests/contract -q
5 passed, 24 warnings in 1.93s
$ cd frontend && npm run build && npm run test
✓ built in 238ms ; Test Files 12 passed (12) / Tests 94 passed (94)
```

**并发干扰说明（重要）**：本 worktree 有另外两个 agent 同时在跑
`memory_test` 集成测试，conftest 每个测试 `TRUNCATE` 用户表，两个 pytest 进程会互相
清库/抢锁。我遇到过不同组合下随机的失败与 `DeadlockDetected: ... TRUNCATE ...`
（服务器 `pg_stat_activity` 当时无其他会话，是瞬时冲突）。**同一命令在对方静默时重跑
即全绿**（例如上面 24 passed 那次，以及单文件 6/10/8 全绿各多次）。因此合并前请在
**独占** `memory_test` 的环境下再跑一次
`scripts/ci-local.sh backend-integration`。

---

## 8. 发现的其它问题（未改，供主控决策）

1. **`begin_batch_member` 函数内没有二次防御**（review I-8 原文要求）。`batch.py` 属另一个
   agent，我只在仓储层过滤。若要求函数内也判 `status == 'pending_batch'`，请转给该 owner。
2. **`_sweep_batch_runs` 在 `get_operation` 返回 `None` 时不 `continue`**（review §5 调度项
   最后一条，低概率加固）：run 有 `operation_id` 但行已被删时会继续走"无剩余证据就关 run"。
   这属于我没被分配的第 6 项，未改（一行 `continue` 即可）。
3. **`memory_prime_degraded` 是唯一的 prime 降级 flag**：summary 截断、目录条数截断、
   目录预算截断都映射到它，只能靠日志（`memory_prime_index_truncated: kept=...`）与
   `index_entries_truncated` 字段区分。若要区分到 flag 粒度，需要扩展
   `DegradedFlag`（对话契约，属主控）。
4. **prime 的 summary 没有 token 级裁剪**：服务端只有 4000 字符硬上限；预算被 summary
   吃满时目录会被整段裁掉（已置标记）。若认为 summary 也可能撑爆预算，可考虑在
   `trim_prime_to_budget` 里也对 summary 做 token 截断（会改变"摘要完整性"语义，未擅自做）。
5. **`PRIME_INDEX_ENTRIES_MAX=200` 是常量而非 setting**：与 `PRIME_SUMMARY_MAX_CHARS`
   同风格。若希望运维可调，需要新增 `MEMORY_PRIME_INDEX_ENTRIES_MAX` 环境变量（未做，
   避免无谓的配置面扩张）。
6. **review 的 `api/dependencies.py:210/262` 行号已过时**：当前 revision 只有一处
   `memory_operations_total` 发射点。
7. **`memory-rebuild-deviations.md` 需要订正**（ADD-047 声称"条数上限交由 conversation 侧
   token 预算裁剪负责"——修复前那段代码不存在；ADD-066 的"cancelled → 释放成员"此前只在
   `complete_operation` 实现）。按要求我**没有**改这个文件，请主控统一订正。
