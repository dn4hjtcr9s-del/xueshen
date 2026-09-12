# 复审报告：`685bfc6` 对 review 意见的修复结果

- **复审对象**：`codex/memory-rebuild-implementation`，HEAD `685bfc6`（`fix(memory): 按外部 review 修复 3 个 Critical + 12 个 Important`）
- **基线**：`2721ef4`（被 review 的原始提交）
- **改动规模**：1 个提交、48 个文件、+6264 / −282 行
- **复审方式**：重跑全部门禁；对 3 个 Critical 逐个**独立复现**（自写探针，不依赖作者新增的测试）；对 12 个 Important 逐个核对源码；另做 3 个方向的深挖（rollout / 记忆写路径 / 记忆读路径）交叉验证
- **本文档只回答问题「修好了吗、有没有引入新 bug」**；上一轮的问题清单见 `REVIEW-memory-rebuild-implementation.md`
- **⚠️ 本版为修订版**：三个方向的深挖验证（rollout / 记忆写路径 / 记忆读路径）完成后发现了首轮遗漏，§0 与 §4 已更新。**请以本版为准。**

---

## 0. 结论（修订版）

**3 个 Critical 全部彻底修复并经独立复现；12 个 Important 主体落地。但修复本身引入了 1 个 Critical 回归，另有 4 个 Important 残留。**

修复的方向、取舍、登记纪律都不错，但有一个反复出现的模式值得点出：**"修一处、漏同类"**——同一缺陷类别在别处仍有实例，而新增的回归测试只覆盖被点名的那一处。本次因此漏掉了 3 个降级标记、2 个投影站点、1 个 KG 调用点。

| 维度 | 结论 |
|---|---|
| 门禁 | 全绿（ruff / format / mypy 309 files / unit 1016 / contract 5 / integration 463+3skip / frontend lint 0 error + build ✓） |
| 3 个 Critical | **全部修复**，且我用自写探针独立复现了"修复前会坏、修复后正常" |
| 12 个 Important | 主体落地；**I-3 的实现引入了新 Critical**，**I-2 / I-11③ 只做到一半** |
| **由修复引入的回归** | **1 个 Critical**（多节点提交把所有 KG 映射置为 inactive，已实测复现）+ 3 个 Minor |
| 未修完的 Important 残留 | 4 个：3 个降级标记、restore/index.md 的 title、KG 审计锚仍绑成员、迁移回填只覆盖升级分支 |
| 默认部署（flag 全关） | **无回归**（三方独立核对一致） |

---

## 1. 门禁实测（本机，与作者报告一致）

| 门禁 | 命令 | 结果 |
|---|---|---|
| Ruff check | `.venv/bin/ruff check backend tests` | ✅ All checks passed |
| Ruff format | `.venv/bin/ruff format --check backend tests` | ✅ 514 files already formatted |
| mypy strict | `.venv/bin/mypy backend` | ✅ Success: 309 source files |
| 单元 stage | 按 `ci-local.sh` 的 `pytest tests/unit + OCR` | ✅ **1016 passed** |
| 契约 | `pytest tests/contract` | ✅ **5 passed** |
| 集成 stage | 按 `ci-local.sh` 的 `pytest tests/integration + failure_recovery + conversation + community + study` | ✅ **463 passed, 3 skipped** |
| 前端 | `npm run lint` / `npm run build` | ✅ 0 errors（3 条既有 react-refresh warning）/ build ✓ |
| 迁移 | 两条链 `upgrade head`；`alembic heads` | ✅ 各单 head（memory `0011`、conversation **`0009`** 新增） |

**两点环境说明（非代码问题）**

1. 若把 `tests/unit` 与 `tests/integration` 放进**同一个** pytest 进程，会出现 6 个 `import file mismatch` 收集错误——这是 pytest 无 `__init__.py` 时按 basename 导入的固有行为，`scripts/ci-local.sh` 本来就分两个 stage 调用，且 `main` 上已存在同款重名对（`test_break_glass.py`、`test_knowledge_summary_phase6.py`）。**不是本分支引入的缺陷。**
2. 有 5 个测试会 `subprocess` 调 `uv run alembic`（`test_backup.py` 4 个 + 新增 `test_rollout_seg_retry.py` 2 个迁移测试）。在 `uv` 缓存不可写的环境里会失败；把 `UV_CACHE_DIR` 指到可写目录后**全部通过**（实测 `test_rollout_seg_retry.py` 8 passed）。属既有模式的环境依赖。

---

## 2. 三个 Critical：逐个独立复现

### C-1 令牌 scope —— ✅ 已修复

**独立验证**（不依赖作者的测试）：直接读取 worker 的签发常量并核对白名单。

```
MEMORY_AGENT_SCOPES = ('memory:context', 'memory:read')
includes memory:read?  True
all scopes within AGENT_ALLOWED_SCOPES? True
```

- 签发点已从内联闭包抽成模块级 `issue_memory_context_token`（`backend/conversation/worker/main.py:40`），可测性提升；
- `backend/auth/context.py` 的 `AGENT_ALLOWED_SCOPES` 本就允许 `memory:read`，因此这是一行就能修对的改动；
- 新增 `tests/integration/test_memory_tool_token_scope.py`（192 行）用**真实签发令牌 + 真实 JWT 校验 + 真实 HTTP 栈**打三个端点，并带**反向护栏**（只带 `memory:context` 的旧令牌必须 403）——这正是我上一轮指出的"测试盲区"被补上了。

### C-2 批次 fencing —— ✅ 已修复

**独立验证**：我自写探针直接查库 + 调用新引入的解析函数，证明修复是**承重**的：

```
batch op = d748dc50-…
member   = 0456cb25-… status='pending_batch' locked_by=None lease_generation=0
member rows satisfying OLD CAS predicate = 0        ← 旧谓词恒 0 行（LeaseFencedError 的根因）
_commit_fencing_operation_id -> d748dc50-…（批次）
points at BATCH operation?    True
single-chain state -> None                          ← 单条路径逐字不变
```

设计正确：新增 `fencing_operation_id`（缺省 = `operation_id`），**CAS 打真正持租的批次行**，而 `memory_commits.operation_id` / mutation 重放键 / evidence 绑定仍绑成员——可追溯性与幂等语义都没丢。`summary.py:828`、`consolidation.py:524` 两处调用点已核对。新增 `tests/integration/test_memory_batch_worker_e2e.py`（378 行）走 **`claim_operation → Worker._execute`** 真实执行入口（此前所有测试都用 `runner.run(batch)` 不带 fencing）。

### C-3 热缓存删除 —— ✅ 已修复

**独立验证**：新增的 `delete_hot_segment_file`（幂等、文件系统错误**不吞**）与 `HotSegmentDeleter` 协议；删除/retention/CLI 三条路径都接了。

- `local_path_for_object_key` 的路径推导与 reader（`reader.py:87`）、writer（`file_naming.segment_path`）**同源**，且经 `_validate_key` 拒绝绝对路径与 `..`，不存在删错文件的路径穿越风险；
- 新增 `tests/integration/test_rollout_hot_cache_deletion.py`（461 行，5 项）核心断言正是 `assert not ids["hot_path"].exists()`——我上一轮指出"既有断言只看 `objects/` 镜像"，这个盲区已被补上；
- I-6 同源修复到位：缺对象存储时**拒绝落 tombstone** 并让可重试 Job 下一轮再来（`thread_deletion.py:92-107`），而不是"没删数据却宣称删了"。

---

## 3. 十二个 Important：核对结果

| 项 | 状态 | 核对要点 |
|---|---|---|
| I-1 段重试登记 | ✅ | 新增迁移 `0009` 把 `(thread_id, ordinal_start)` 改成**部分唯一索引**（`WHERE status <> 'deleted'`），并**先建索引再删旧约束**；`open_turn` 复用非删除段（换新 `segment_id`，因为 `put_immutable` 禁止同 key 异内容）；`register_open` 返回 `RegistrationStatus`（inserted/already_open/failed），登记失败**不写任何东西**，从结构上消灭孤儿对象。新增 862 行 `test_rollout_seg_retry.py`（8 项，含"登记失败不产生孤儿对象"与"1 deleted + 1 活跃 可共存、第二个活跃行仍被拒"） |
| I-2 consolidation 取错 operation | ⚠️ **部分** | summary 重写（meta 幂等凭证）与悬空链接 sightings 已改对；**但 `consolidation.py:759` 给 `kg_dual_write.after_consolidation(operation_id=…)` 传的仍是 `_operation(state).operation_id`（最后一个成员）**，而该参数会进 `graph_state_audit.operation_id`（实测确认）→ KG 审计锚仍绑成员。见 §4 新发现 8 |
| I-3 KG 映射被摧毁 | ❌ **引入新 Critical** | 三分支设计正确、`frontmatter_patch` 场景确实修好了；但"带节点"分支写成 `for kept in node_ids: deactivate(except_node_id=kept)`，**多节点时后一次调用会把前一次保留的节点也置 inactive**。实测：双节点提交后 `active links = 0 / 2`。见 §4 新发现 1（Critical） |
| I-4 read 续读提示 | ✅ | 重构为 `_render_bounded`：**按行**二分裁剪后用裁剪过的 content 重渲染，`_attach_read_hint` 用回传的**已投递行数**；深挖方用 500 行文档链式验证 offset 0→167→334、连续前缀、0 重复。残留：预算连一行都放不下时会提示回**同一** offset（见 §4 新发现 11） |
| I-5 prime 无界 | ✅ | 两侧加界：服务端 `PRIME_INDEX_ENTRIES_MAX=200` + 新增 `index_entries_truncated` 字段（已进 OpenAPI 快照）；节点侧 `trim_prime_to_budget` 保前缀、置标志、不原地修改；对**旧 checkpoint** 里的未裁剪 prime 在视图出口再兜一次。残留：预算扣减只作用于目录、不含 summary（见 §4 新发现 10） |
| I-6 flag 关闭仍 tombstone | ✅ | 对象存储构造脱离 flag；缺存储则 `ROLLOUT_OBJECT_STORE_MISSING` + 保持 deleting。残留：**`object_key` 为 NULL 的未封存段被整段跳过**（见 §4 新发现 6） |
| I-7 kodo 只在 CLI 生效 | ✅ | worker 与 app 都改走 `build_rollout_object_store`；app 的 4 处 `get_settings()` 改用 `create_app` 入参 |
| I-8 取消不释放 / 已取消仍被处理 | ✅ | `request_cancel` 复用 `settle_batch_members(status="cancelled")`（未写第二套逻辑）；`list_batch_member_operations` 加 `status='pending_batch'` 过滤；`running` 分支故意不释放（终态由 `complete_operation` 走同一套 settle）。连带引入一条误报 warning（Minor） |
| I-9 无成员级失败隔离 | ✅ | `runner._guard_member_node` 逐节点包裹 + 条件边短路；**保留每节点一个 superstep**（崩溃恢复粒度不变，这是关键的架构判断）；`LeaseFencedError` 与环境类异常（`MEMBER_FATAL_EXCEPTIONS`）**照旧上抛**，不误判成成员失败；失败成员释放回证据池。残留：**全部成员都失败时批次仍报 `succeeded`**（Minor） |
| I-10 降级标记不在 Literal | ⚠️ **部分** | `answer_stream_*` 三个已补（实测 `validate_event_payload` 通过）。**同类还有 3 个未补**：`rewrite_structured_fallback` / `rewrite_plan_contract_invalid` / `evidence_structured_fallback`。见 §4 新发现 2 |
| I-11 投影三缺陷 | ⚠️ **部分** | ① 迁移处理器接线 `refresh_index_projection`，但**只在"本次升级了文档"的分支**执行，已 v2 文档永远补不上；② `related_topic_keys` 改由 `_links_of_rendered` 现算，**这一项完全修好**；③ 提交与 refresh 路径已取 frontmatter `name`，但 **restore（`:1220`）与 index.md（`:1520`）两处未改** → restore 后 title 回退、index.md 与注册表永久不一致。见 §4 新发现 7/9 |
| I-12 前端 action 联合类型 | ✅ | `frontend/src/api/memory.ts` 已补 `"frontmatter_patch"`；lint/build 通过 |

**默认部署（新 flag 全关）无回归**：`build_rollout_object_store` 的 `local` 分支只做 `Path(root)` 拼接、不建目录不校验凭据；`kodo` 分支的缺配置校验与 `settings.py:864` 的启动强校验**同源**（不会把一个原本能启动的部署变成启动失败）；单条路径的 fencing 缺省值为 `None`→退回 `operation_id`，行为逐字不变。

---

## 4. 新发现

> 编号已整理去重（首轮报告 + 三方深挖验证的合并结果）。**★ 标记的是由本次修复直接引入的问题。**

### 新发现 1 ★（**Critical，由 I-3 修复引入**）：多节点提交会把**所有** KG 映射置为 inactive

| 项 | 内容 |
|---|---|
| 位置 | `backend/memory/services/memory_service.py:845-849`（循环）+ `backend/memory/persistence/graph_states.py:340-354`（被调函数） |
| 证据 | `for kept_node_id in node_ids: await gs_repo.deactivate_graph_links(session, ..., except_node_id=kept_node_id)`；而 `deactivate_graph_links` 的 SQL 是 `UPDATE memory_graph_links SET active=false WHERE user_id=… AND memory_id=… AND (except IS NULL OR node_id != :except)`——**没有版本谓词**。`node_ids=[A,B]` 时：第一次（except=A）把 B 置 false，第二次（except=B）把 A 置 false |
| 实测（自写探针，真实 PG） | 双节点提交 → `node=n7711 v=1 active=False`、`node=n7712 v=1 active=False`，**`active links = 0 / 2`**。修复前的旧代码（先全灭再逐个 upsert）在这里是**正确**的 → 确认是回归 |
| 后果 | 该记忆的全部 KG 映射在提交后立刻失效 → 紧随其后的 `_dual_write_kg`（要求 `active=true AND memory_version=:version`）必然 `no_graph_mapping`；`/knowledge-graph/recommendations` 丢失该主题；"无节点提交"分支读 `previous_links` 得空集，**永远无法自愈**。全程只有 status 字段变化，无告警 |
| 触发概率 | 当前唯一调用方 `summary.py:837` 每个 plan 只产出 ≤1 个节点 → **暂时潜伏**；但 `graph_nodes` 本就是列表、KG 映射是多对多，且分支自己的测试就以双节点为预期（只是断言写漏了 active）。一旦有 plan 带 2 个节点即刻命中 |
| 为什么测试没抓到 | `tests/integration/test_mastery_kg_links_and_projection.py:237` 只断言 `{row["node_id"]} == {NODE_ID, other_node}`（**行集合**），两个 inactive 行同样满足；紧随其后的单节点提交又把 `NODE_ID` 重新激活，后一行断言也照样通过 |
| 建议修复 | 改成一次调用 + 集合排除（`node_id <> ALL(CAST(:ids AS text[]))`）；把该测试断言改成"每个计划内节点都 `active=True`" |

### 新发现 2（Important）：同类"封闭 Literal"缺陷仍有 **3 个**未补

- 生产端：`graph/nodes/rewrite.py:81`（`rewrite_structured_fallback`）、`:96`（`rewrite_plan_contract_invalid`）、`graph/nodes/evidence.py:302`（`evidence_structured_fallback`）
- 证据：把**所有**降级标记字符串与 `DegradedFlag` 做集合差 → 上述 3 个都不在 Literal 里；实测 `AnswerCompletedPayload(degraded_flags=["rewrite_structured_fallback"])` 抛 `ValidationError`（`evidence_structured_fallback` 同样）
- 后果与 I-10 完全一致：标记经 `state.py` 的合并 reducer 进 `state["degraded_flags"]`，`finalize.py:54` 原样喂给 `build_answer_completed_payload` → 在 **finalize 事务内** `validate_event_payload` 抛错且无 try/except → **整个事务回滚、已流出的回答丢失**
- 触发条件是**常规降级路径**（改写或证据结构化输出回退），不是异常路径
- **main 上既有**，非本次引入；但本轮既然在扩展这个 Literal，建议一次补齐并加一条**元测试**（从 `backend/conversation/` 提取所有 append 字面量 vs `get_args(DegradedFlag)`），否则会第三次遗漏

### 新发现 3 ★（Minor，由 C-2 修复引入）：`commit_started_at` 在批次行上残留

- 位置：`backend/memory/services/memory_service.py:566-572`（mark）与 `:766-773`（clear）
- 证据：`_mark_commit_started(...)` 传了 `fencing_operation_id=cas_operation_id`，但 `finally` 里的 `_clear_commit_started(...)` **没有传** → clear 的 CAS 打在**成员行**（`locked_by` 为空的 `pending_batch` 行）上，恒 0 行
- 实测：绕开 `Worker._execute` 的前置 clear 直接跑图后 `BATCH row commit_started_at = 2026-09-12 03:45:23+00`（残留）；完整 `_execute` 路径下为 `None`（Worker 下次执行前会顺带清掉）
- 影响：功能影响小（`request_cancel` 对 `running` 本就拒绝取消），属"标记语义被破坏 + 观测噪声"
- 建议：`_clear_commit_started` 同样传 `fencing_operation_id=cas_operation_id`（一行）

### 新发现 4 ★（Minor，由 I-1 修复引入）：序数范围复用 + 登记/改名非原子

- **序数重叠**：`next_ordinal_start` 用 `max(COALESCE(ordinal_end, ordinal_start))`，而 **open 段的 `ordinal_end` 为 NULL** → 只按 `ordinal_start` 计数。实测：open 段文件里 ordinal 已用到 2，`next_ordinal_start()` 仍返回 1 → 两段范围重叠（深挖方复现出 `(0,3)` 与 `(1,2)`、`read_thread_records` 出现重复/交错 ordinal），与 `sealer.py:21-23` 自己的承诺矛盾，且没有 reconcile 类别能发现
- **登记先于改名**：`sealer.register_open` 在同一 `try` 内先提交 manifest 事务（标废旧段 + 插新行）、**再**改名热文件；改名 `OSError` 被归类为"登记失败"，于是 DB 已变更却按失败处理 → 调用方停止记录，留下 `open_local_missing` 行 + 一个不再被任何活动行引用的改名后文件（不可发现的残留）
- 建议：序数起点改用 open 段热文件的真实最大 ordinal（或 append 时持久化 `ordinal_end`）；把改名移出提交判定或失败时补偿

### 新发现 5 ★（Minor，由 I-1 修复引入）：一次登记失败会把记录器整体降级

- 位置：`backend/conversation/rollout/recorder.py:666`（`self._degraded = True`）与 `:266/:390/:476` 读取点
- 证据：`_ensure_registered` 失败后置的是**进程级、永不重置**的 `_degraded`（docstring 说的是"本 turn 停止记录"）；`open_turn` 直接返回 `None`、`record()` 恒 `False`、`flush()` 直接返回；全文件只有 `:189` 一处 `= False`
- 后果：一次**瞬时** DB 错误就会静默停掉该 worker 上**所有** thread 的 rollout 写入，直到重启；`verify-manifest` 看不到（没有不一致，只是没数据）
- 建议：降级收敛到单个 turn（`open_turn` 时重置 / 登记成功后清除），或改成失败计数 + 退避

### 新发现 6（Important）：`object_key IS NULL` 的未封存段被删除路径整段跳过

| 项 | 内容 |
|---|---|
| 位置 | `backend/conversation/services/thread_deletion.py:87-92`（`rollout_keys = [... if row.get("object_key")]` + `if rollout_keys:`）、`cli/rollout.py:255-281` |
| 证据 | `rollout_keys` 只收有 `object_key` 的行；从未封存的段（`status='open'`、`object_key IS NULL`）得到**空** `rollout_keys` → 整个对象/热段删除块被跳过，**连 tombstone 都不落**，Job 直接 `done` |
| 实测（自写探针，真实 FS + 真实仓储查询） | 建一个 open 段并写入含用户原文的 `user_message`（`[manifest rows] [('open', None)]`）→ thread_deletion 用的查询给出 `rollout_keys = []` → 删除后热文件仍在、`[still contains user text] True` |
| 后果 | **"删了 thread，用户原文仍在磁盘上"**——与 C-3 完全同类，触发条件是"段未封存"（turn 崩溃 / 封存失败留下的热段）；而热段路径是**可推导的**（thread_created_at + ordinal_start + segment_id 都在行里） |
| 对上一轮结论的修正 | 我的 C-3 探针只走了"已封存段"（新增测试也是），没覆盖 `object_key IS NULL`。**C-3 应判为"部分修复"** |
| 建议 | 对无 `object_key` 的行按 `segment_path(...)` 推导热段路径并删除；并**无条件**落 tombstone（该 thread 的行全部标 deleted） |

### 新发现 7（Important）：I-11③ 只改了一半，`restore` 与 index.md 仍用 `topic_title`

- 位置：`backend/memory/services/memory_service.py:1220`（restore：`"title": parsed.topic_title`）与 `:1520`（`rebuild_index`/index.md 用 `doc["topic_title"]`）
- 后果：① **撤销删除后注册表 title 回退**成 `topic_title`（`parsed.name` 就在手边却没用），`memory.search`/prime 返回旧标题；② index.md `- title` 与注册表/prime 的 title **永久不一致**（与已登记的 DEV-023 `description` 同类，但未登记）；③ 四处投影站点的 `search_text` 都不含 `name`，重命名后的 v2 文档失去 `title` 精确/前缀召回分支
- 建议：两处都改用与提交路径相同的 `(doc.name or doc.topic_title) if _is_v2(doc) else doc.topic_title`，并把 `name` 纳入 `search_text`；补一条"重命名 → 删除 → 恢复"的集成测试

### 新发现 8（Minor）：I-2 未修完 —— consolidation 给 KG 双路更新传的仍是成员 operation

- 位置：`backend/memory/graph/consolidation.py:759`：`operation_id=_operation(state).operation_id`
- 证据：该参数会进 `graph_state_audit.operation_id`（`kg_dual_write` → `service.apply_projection(operation_id=…)` → `graph_states.py:145` 的 INSERT 列）——我逐跳核对确认
- 与 I-2 目标不一致：summary meta 与悬空链接已用批次 id，KG 审计锚仍绑最后一个成员 → 同一批次 provenance 是"混合锚"。幂等键与 uuid5 追溯锚用的是 `batch_operation_id`（正确），故只影响"审计行按哪个 operation 回查"
- 建议：传批次 id，与函数契约注释（`operation_id: UUID,  # 批次 operation`）一致

### 新发现 9（Minor）：迁移回填只覆盖"本次升级"的文档

- 位置：`backend/memory/graph/maintenance.py:429-431`：`if upgraded is None: skipped_already_v2 += 1; continue` 位于 `refresh_index_projection` **之前**
- 后果：**已被更早版本迁移过、或由 `frontmatter_patch` 原生成为 v2 的文档永远补不上投影**，运维"再跑一次任务"是 no-op；0008 docstring 承诺的回填对这部分文档不成立
- 建议：`skipped_already_v2` 分支同样刷新投影（非 dry-run），并在迁移集成测试里断言投影行

### 新发现 10（Minor）：prime 预算不覆盖 summary，"共用预算"的说法不成立

- 位置：`backend/conversation/services/context_service.py:279-293`
- 证据：`entries` 为空/缺失时**提前 return**（不置截断标志），即使 `count(summary)` 单独超预算；有 entries 时 `used = count(summary)` 起步，而 `PRIME_SUMMARY_MAX_CHARS=4000` 的 summary 本身就可能吃掉整个 3000 token 预算
- 另外：prime 与 memory 工具结果**各自**拿满 `conversation_memory_token_budget`，实际注入可达快照声明的 `budgets.memory_tokens` 的约 2 倍 → docstring 里"共用预算才能让 `budgets.memory_tokens` 与实际注入量一致"的论证不准确
- 建议：summary 也纳入同一预算并按需截断/置标志；docstring 改为"同一预算族"

### 新发现 11（Minor）：预算连一行都放不下时，read 提示指回**同一** offset

- 位置：`backend/conversation/graph/nodes/memory_tool.py:435-438` + `:550-553`
- 实测（深挖方探针，budget=200）：第一轮返回 stub，hint 的 `line_offset` 与请求的 167 **相同**；由于 `reason: "memory_tool_budget_exhausted"` 保留，且重复调用会命中幂等缓存（轮数上限 6），所以**不是**原来的"跳过未投递行"、也不会无限循环，但会浪费剩余轮数
- 建议：`delivered == 0` 时不返回 offset 提示

### 新发现 12（Minor）：批次内**全部**成员失败时仍报 `succeeded`

- 位置：`backend/memory/graph/batch.py:393`：`errors = [] if mutations else list(state.get("errors") or [])`
- 证据：成员级隔离只写 `batch_member_error`、不写 `state["errors"]`，因此"全部成员被隔离"时 `errors` 为空 → `manager.normalize_result` 判 `succeeded`。成员已释放回池子（数据不丢），但 operation 状态与实际写入量不符，且无 needs_review/dead_letter 信号
- 建议：`mutations == [] and failed` 时强制 `needs_review`（或写显式标记）

### 新发现 13（Minor）：被释放的失败成员没有尝试计数，理论上可永久循环

- 位置：`backend/memory/persistence/operations.py:722-745`（`release_batch_member`）、`graph/batch.py:386-448`
- 说明：失败成员清掉 `batch_operation_id` 回池子，只把 `next_run_at` 推到 `now()`。**没有 attempt 计数、没有终态出口**——确定性失败的证据会每晚重新入批、每晚失败、每晚再被释放，持续消耗 LLM 预算直到人工介入
- 可以接受的理由：这是"宁可重试也不永久丢用户数据"的有意取舍，且每次都有公开 warnings 可观测；但缺一个与 `max_attempts` 对齐的升级出口
- 建议：失败计数超 N 次转 `dead_letter` 进人工审核

### 新发现 14（Minor）：取消成员后会误报"成员在库中无归属"

- 位置：`backend/memory/graph/batch.py:136-140`：新增的 `status='pending_batch'` 过滤把**已取消**的成员藏了起来，而 `payload_ids` 仍声明它们 → 误报 `批次 payload 声明的 N 条成员在库中无归属，已忽略`
- 性质：I-8 过滤带来的**误报**（成员仍归该批次，只是被取消），会误导运维
- 建议：把 `cancelled` 从声明集合差异里排除，或改措辞

### 新发现 15（Minor，作者已登记）：kodo 模式下热缓存文件仍不删

- `QiniuKodoRolloutObjectStore` **没有**实现 `HotSegmentDeleter`，`delete_hot_segment()` 返回 `False`；而 recorder 在 kodo 模式下**仍然**把段写在本地 `conversation_rollout_root`。于是 kodo 部署里删 thread 只删云端对象，本地 `{root}/threads/...` 的用户原文仍在磁盘上
- docstring 已诚实写明"返回 False 不等于本机磁盘上没有热文件"，但调用方把它当"无此能力"处理，没有补偿路径
- 影响面：只在 kodo 部署出现（默认 local、生产配置也未启用 rollout）。属 C-3 在 kodo 分支上的**残留**
- 建议：让 kodo 实现也删本地热段，或提供独立的本地热缓存清理命令

### 新发现 16（Nit）：重构后遗留死路由函数

- `backend/memory/graph/batch.py:482 route_after_summary_finalize` 在生产代码里**已无调用点**（被 `runner._route_after_finalize_summary_result` 取代，仅剩一处 docstring 提及 + `tests/unit/test_memory_batch_graph.py` 仍在测它）。建议删除或改为委托，避免"测着一个不生效的路由"。

### 新发现 17（Nit）：`resolve_resume` 的 fencing 参数未被使用

- `sealer.py:167-176` 接收 `fence` 但从未使用；`mark_deleted` / `discard_open` 不像 `seal()`（`rollout_manifests.py:195-205`）那样带 `EXISTS(... lease_owner …)` 守卫。理论上一个已失租的 worker 仍能 tombstone 当前持有者的活动段并清空其消息指针。属新增变更（标废/丢弃）未纳入既有 fencing 纪律，建议补守卫或删掉该参数并注明"有意不 fence"

---

## 5. 上一轮 Minor 的处置

作者把上一轮全部 Minor/Nit 诚实登记为 **DEV-038～DEV-057**（`memory-rebuild-deviations.md`），并明确写"本轮实际修复的是 review §7 的 10 项必改清单；DEV-049~057 属 Minor/待决，登记但未在本轮修改"。我抽查了其中几项确认**登记与代码一致**：

- `turn_started` 仍只在契约/policy 白名单里、无生产写入点（DEV-038 ✅ 准确）
- `conversation_rollout_retention_days` 仍无人读取、CLI 仍要求 `--older-than-days`（DEV-039 ✅ 准确）
- `runtime.rollout_reader` 仍只在 `app.py` 为 source-read 服务构造局部实例，conversation runtime 从不赋值（DEV-041 ✅ 准确）
- `MEMORY_SUMMARY_LLM_CONCURRENCY` 仍无消费者、批内成员仍串行（DEV-040 ✅ 准确）
- `.env.example` 仍缺 `MEMORY_CONSOLIDATION_INPUT_MAX_CHARS`（DEV-053 ✅ 准确）

**这种"修完必改项、把剩余项逐条登记并注明未改"的处置是恰当的**，没有把未修项伪装成已修。

**但登记本身有两处不准确**（深挖发现）：

- **ADD-047** 正文仍写"冻结契约里没有 `index_entries_truncated` 字段，服务端按'不静默丢条目'**全量返回**"——而本轮恰好新增了该字段与 200 条上限；只有汇总表加了一行说明，正文没改。下一个读者无法判断哪份是权威
- `worker/job_worker.py:35-37` 的注释仍写"为 None 时跳过对象删除（见 thread_deletion）"，而 thread_deletion 现在**拒绝** tombstone 并重试；这条注释会把人引回不安全的老路径

---

## 6. 判定（修订版）

| 问题 | 回答 |
|---|---|
| 修改结果是否到位？ | **主体到位**：3 个 Critical 全部彻底修复并经独立复现；12 个 Important 里 8 个完全修好、4 个只做到一半（I-2 / I-3 / I-10 / I-11）。 |
| 是否引入新 bug？ | **引入 1 个 Critical**：多节点提交把所有 KG 映射置为 inactive（I-3 的实现缺陷，已实测复现；因当前每 plan ≤1 节点而潜伏）。另有 3 个 Minor 由修复直接引入（`commit_started_at` 残留、序数重叠、记录器进程级降级）。 |
| 现在的可用性判断 | 三个 Critical 的**原始**问题已解除；但 I-3 引入的 KG 回归必须先修，否则打开 `memory_kg_dual_write_enabled` 后多节点映射会静默失效。 |
| 与上一轮的对比 | 从"新链路一条都不能真正启用"→"启用前需修 1 个 Critical 回归 + 4 个 Important 残留，量都不大"。 |

**一个值得注意的模式**：本轮的修复反复出现"**修一处、漏同类**"——同一个缺陷类别在别处仍有实例，而新增回归测试只覆盖被点名的那一处。本次因此漏掉了 3 个降级标记、2 个投影站点、1 个 KG 调用点、1 条删除路径。建议对这几类都补"全量枚举"型元测试，而不是逐点补。

**建议合并前的最小补充清单（按优先级）**

1. **修 I-3 的多节点回归**：`_sync_graph_links` 改成一次调用 + `node_id <> ALL(:ids)`；测试断言补 `active is True` —— 新发现 1（Critical）
2. **`DegradedFlag` 补齐 3 个值** + "生产者 vs Literal"元测试 —— 新发现 2
3. **C-3 收尾**：删除路径覆盖 `object_key IS NULL` 的未封存段，并**无条件**落 tombstone —— 新发现 6
4. **I-11③ 收尾**：restore 与 index.md 也取 frontmatter `name`；`search_text` 纳入 `name` —— 新发现 7
5. **I-2 收尾**：`consolidation.py:759` 传批次 operation —— 新发现 8
6. 顺手修：`_clear_commit_started` 补传 `fencing_operation_id`；`next_ordinal_start` 对 open 段用真实最大 ordinal；记录器降级收敛到单 turn —— 新发现 3/4/5
7. 订正 ADD-047 正文与 `job_worker` 注释 —— §5 末
