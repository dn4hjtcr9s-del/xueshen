# 报告：封闭枚举 + 多条删除路径（review-2 新发现 2/4/5/6/10/11/15/17）

- **分支**：`codex/memory-rebuild-implementation`（worktree `.local/worktrees/memory-rebuild-implementation`）
- **基线**：`685bfc6`（复审报告 `REVIEW-2-verification-685bfc6.md` 的复审对象）
- **范围**：`backend/conversation/**`（含 `contracts/api.py`、`rollout/**`、`worker/**`、
  `services/thread_deletion.py`、`cli/rollout.py`）+ 我新增/修改的测试
- **未触碰**：`backend/memory/**`（另一 agent）、`memory-rebuild-deviations.md`（题面禁止）、未提交 git
- **硬性交付**：两个全量枚举型元测试（A：降级标记封闭枚举；B：删除路径全量枚举）+ 真库真盘参数化集成测试

---

## 0. 结论速览

| # | 问题 | 状态 | 关键落点 |
|---|---|---|---|
| 新发现 2 | 3 个降级标记不在 `DegradedFlag` | ✅ 修复 + **元测试固定** | `contracts/api.py`、`tests/unit/test_degraded_flag_enum_meta.py` |
| 新发现 6 | `object_key IS NULL` 的未封存段被删除路径整段跳过 | ✅ 修复 + 真值表 + 参数化集成 | `rollout/deletion.py`、`thread_deletion.py`、`cli/rollout.py` |
| 新发现 4① | `next_ordinal_start` 对 open 段给出重叠序号 | ✅ 修复（**改取本地热段真实最大 ordinal**） | `rollout/file_naming.py`、`rollout/sealer.py`、`rollout/recorder.py` |
| 新发现 4② | `register_open` 把改名 `OSError` 当成"登记失败" | ✅ 修复（改名移出提交判定 + 补偿 + `rename_failed` 状态） | `rollout/sealer.py` |
| 新发现 5 | `recorder._degraded` 进程级、永不重置 | ✅ 收敛到单 turn | `rollout/recorder.py` |
| 新发现 11 | 预算连一行都放不下时提示回同一 offset | ✅ 修复（该情形不给可执行 offset） | `graph/nodes/memory_tool.py` |
| 新发现 15 | kodo 模式热缓存删不掉 | ✅ 修复（factory 产出的 store 都带 root 级热删除能力） | `rollout/factory.py`、`rollout/qiniu_kodo.py`、`rollout/object_store.py` |
| 新发现 17 | `resolve_resume` 的 fencing 参数未被使用 | ✅ **删参数**（只读函数）+ 在真正的写路径补守卫 | `rollout/sealer.py`、`persistence/rollout_manifests.py` |
| 新发现 10（主控追加） | prime 预算不覆盖 summary | ✅ 修复（summary 与目录共用同一预算，先扣 summary） | `services/context_service.py`、`graph/nodes/memory.py` |
| （顺带） | `job_worker` 里"为 None 时跳过对象删除"的过期注释 | ✅ 订正 | `worker/job_worker.py:35-38` |

---

## 1. 硬性交付 A：降级标记的封闭枚举元测试

**文件**：`tests/unit/test_degraded_flag_enum_meta.py`（434 行）

### 1.1 提取到了什么

AST 遍历 `backend/conversation/**`（**884 个函数、91 个模块**），收集"写进降级标记集合"的字符串字面量，
并按**去向**分类：

| 去向 | 含义 | 本轮提取结果 |
|---|---|---|
| `state` | 进 `state["degraded_flags"]` 或 `turn.degraded` payload → 最终被 `AnswerCompletedPayload` / `TurnDegradedPayload` 的封闭 Literal 校验 | **14 个**：`memory_unavailable`/`memory_degraded`/`memory_prime_degraded`/`memory_prime_unavailable`/`memory_tool_degraded`/`memory_tool_truncated`/`memory_tool_budget_exceeded`/`citation_degraded`/`answer_stream_interrupted`/`answer_stream_truncated`/`answer_stream_refused`/`rewrite_structured_fallback`/`rewrite_plan_contract_invalid`/`evidence_structured_fallback` |
| `rollout` | 只进 rollout 审计记录（`contracts/rollout.py` 的 `degraded_flags: list[str]`，不是 SSE 契约） | **1 个**：`graph_failed` |
| 未识别形状 | 提取器不认识的写入形状（必须回来更新提取器） | **0 个** |

覆盖的写入形状（都在生产代码里真实存在）：`degraded_flags=[...]`（kwarg）、
`"degraded_flags": [...]`（dict key）、`degraded_flags.append("...")`、
`_emit_degraded(runtime, state, "...")`、`record_rollout(..., {"degraded_flags": [...]})`。

### 1.2 四组断言

1. **正向（硬）**：`state` 去向的字面量集合 ⊆ `get_args(DegradedFlag)`；失败消息**列出缺失项 + 站点**；
2. **反向（硬）**：Literal 里不允许有"永不产生"的死值 → 死值必须在 `NEVER_PRODUCED` 白名单里写明原因，
   且白名单**双向精确**（将来补上生产者后必须删掉白名单项，否则失败）；
3. **旁路（硬）**：只进 rollout 记录的标记必须在 `ROLLOUT_ONLY` 里登记（`graph_failed`），同样双向精确；
4. **形状守卫 + 契约自检 + 端到端**：提取器自检（已知 14 个生产站点必须被提取到，防止元测试"空转"）、
   `AnswerCompletedPayload` / `TurnDegradedPayload` 的 Literal 与 `DegradedFlag` 必须同源、
   每个提取到的标记都真的能构造出合法 payload。

### 1.3 为什么新增同类项时会变红（含演示）

**演示（实跑）**：在 `graph/nodes/evidence.py:302` 临时加一个字面量
`meta_demo_unregistered_flag`（不补 Literal）→ 运行元测试：

```
$ uv run pytest tests/unit/test_degraded_flag_enum_meta.py -q
E  AssertionError: 以下降级标记会进 state/SSE，但不在 DegradedFlag（封闭 Literal）里：
   ['meta_demo_unregistered_flag']。请在 backend/conversation/contracts/api.py 的 DegradedFlag 补齐——
   否则 finalize 事务内的 validate_event_payload 会抛错并回滚整个事务。
   相关站点：["evidence.py:302 [state] 'meta_demo_unregistered_flag'"]
E  assert not ['meta_demo_unregistered_flag']
E  ValidationError: 1 validation error for AnswerCompletedPayload
E  degraded_flags.0  Input should be 'memory_unavailable', ... or 'evidence_structured_fallback'
   [type=literal_error, input_value='meta_demo_unregistered_flag', input_type=str]
2 failed, 5 passed
```

撤销后 `7 passed`（`git diff --stat backend/.../evidence.py` 无输出）。两条断言同时变红：
集合断言（指出缺失项与站点）+ 端到端 payload 校验（**复现 finalize 事务回滚那条后果**）。

**新增死值演示（反向）**：任何"Literal 有、生产端没有"的取值同样会红——
本轮真实命中了 `retrieval_partial` / `retrieval_unavailable`（见 §4.1）。

---

## 2. 硬性交付 B：多条删除路径的元测试

**文件**：`tests/unit/test_rollout_deletion_paths_meta.py`（485 行）+
`tests/integration/test_rollout_deletion_paths.py`（579 行）

### 2.1 提取/枚举到了什么

- **唯一入口清单 `DELETION_ENTRIES`（3 条）**：
  | key | 入口 | thread 级清扫 | tombstone / 拒绝语义 |
  |---|---|---|---|
  | `thread_deletion` | `services/thread_deletion.py::execute_delete_thread` | 必须 | `mark_thread_deleted`；缺对象存储 → `wait_job(ROLLOUT_OBJECT_STORE_MISSING)` |
  | `cli_delete_thread_rollouts` | `cli/rollout.py::_delete_thread_rollouts` | 必须 | `mark_thread_deleted`；有失败项 → 退出码 1、不落 tombstone |
  | `cli_retention_scan` | `cli/rollout.py::_retention_scan` | **禁止** | `mark_deleted`（逐段）；sealed 缺 key 显式失败 |
- **触发入口 `TRIGGER_ENTRIES`（4 条）**：API `DELETE /conversations/{thread_id}`、
  `ConversationService.delete_thread`、`JobWorker._run_delete_thread`、`JobWorker._execute_job`
  ——断言它们**不直接**调用删除原语、且源码里确实出现 `delete_thread`（只入队协调 Job）。
- **新入口自动暴露**：扫描 `backend/conversation/**` 的 **884 个函数**，凡函数体**直接**调用删除原语的
  必须在清单里登记，或在 `NON_ENTRY_ALLOWLIST`（10 项，每项带原因）里说明为何不是入口。
  本轮实际候选 13 个 = 3 个登记入口 + 10 个白名单项（`deletion.py` 5 个助手、
  `object_store.py`/`qiniu_kodo.py` 5 个能力实现）。
- **CLI 子命令守卫**：解析 `cli/rollout.py` 的 AST，取出所有带 `--apply` 的子命令，
  与登记表精确比对：`delete-thread-rollouts` / `retention-scan` 必须登记，
  `reconcile-orphans` 必须在 `CLI_NON_ENTRY_MUTATIONS` 里写明"只 tombstone、不删对象"的原因。
- **集成覆盖守卫**：登记表里每个 key 必须在 `tests/integration/test_rollout_deletion_paths.py` 里出现
  （防止"登记了但没真跑"）。

### 2.2 三段齐全的判定方式

对每个入口取**传递调用闭包**（AST 函数级调用图，按短名合并同名函数，宁可多报不可漏报），断言：

- 闭包 ⊇ `{delete_rollout_objects, delete_rollout_hot_segments}`（对象 + 热段两份载体都要删）；
- thread 级入口闭包 ⊇ `sweep_thread_hot_segments`；**段级入口（retention）闭包必须不含它**
  （防止"清理一个过期段"顺手删掉同 thread 未过期的段）；
- 闭包 ∩ `{mark_thread_deleted, mark_deleted, wait_job}` ≠ ∅（落 tombstone 或显式拒绝）。

> 口径说明（写在测试 docstring 里）：**新入口检测用"直接调用"**（精确、零误报），
> **三段齐全用传递闭包**（严格、不误报）。最初两者都用传递闭包时，`run_forever` /
> `_poll_once` / `main` 这类枢纽函数把整个 worker 连成一张网，元测试退化成白名单噪音。

### 2.3 为什么新增同类项时会变红（含演示）

**演示 A（新增"只删一半"的入口）**：在 `cli/rollout.py` 临时加一个只调
`runtime.object_store.delete(...)` 的新函数 `_purge_thread_objects`：

```
$ uv run pytest tests/unit/test_rollout_deletion_paths_meta.py -q
E  AssertionError: 发现未登记的 rollout 删除路径：
E      rollout.py::_purge_thread_objects（直接调用 ['object_store.delete']）
E  若它确实会删除 rollout 段/对象或落 thread 墓碑，请加进 DELETION_ENTRIES 并补齐集成用例
E  （tests/integration/test_rollout_deletion_paths.py）；若它只是内部助手或能力实现，
E  请加进 NON_ENTRY_ALLOWLIST 并写明原因。
```

**演示 B（段级删除顺手做 thread 清扫）**：给 `_retention_scan` 加一次
`sweep_thread_hot_segments(...)`：

```
E  AssertionError: CLI retention-scan --apply 是**段级**删除，不得按 thread 清扫目录——
   那会连带删掉同 thread 内未过期的段。
E  assert 'sweep_thread_hot_segments' not in {...}
```

两处演示都已撤销，撤销后 `7 passed`（`grep _purge_thread_objects backend tests` 无命中）。

### 2.4 参数化集成测试（真库真盘）

`tests/integration/test_rollout_deletion_paths.py`，11 个用例：

1. **真值表**（新发现 6 的核心）：`object_key` 空/非空 × 热文件存在/不存在 **四种组合**，
   每种都断言"热文件不存在 + 对象镜像清空 + 全部 manifest 行 `deleted` 且写了 `deleted_at`
   + 整个 rollout 根目录搜不到用户原文"；
2. **未封存段 + 缺对象存储** → 必须 `wait`、保持 `open`、文件仍在（"不说谎"）；
3. **远端 store + 本机热缓存**（新发现 15）：对象放"远端"（内存 store）、热段在本机，
   删除后远端对象与本机热段都消失；
4. **三个登记入口各一个用例**（用例名里带登记 key：`thread_deletion` /
   `cli_delete_thread_rollouts` / `cli_retention_scan`），CLI 用真实 `_open_runtime()` +
   真实 `conversation_test` 库 + 临时 rollout 根目录；
5. **作用域断言**：thread 级删除不得影响另一个 thread 的未封存热段；
   retention 不得动 `open` 段（`status` 仍为 open、热段仍在）；
6. **reconcile-orphans 的边界**：只 tombstone、不删对象/不清热段（与 CLI 白名单口径一致）。

---

## 3. 逐项修复说明（含方案取舍）

### 3.1 新发现 2：补齐 3 个降级标记

`DegradedFlag` 追加 `rewrite_structured_fallback` / `rewrite_plan_contract_invalid` /
`evidence_structured_fallback`，注释写明"由元测试固定，不再依赖人工逐点补"。
**未改任何公开 schema 形状**（SSE 事件类型、payload 字段都没变），因此 OpenAPI 快照无需重生成
（`tests/contract` 5 passed，快照与 HEAD 一致）。

### 3.2 新发现 6：未封存段也要能删

新增 `backend/conversation/rollout/deletion.py`（176 行），把删除动作收敛成两个统一助手：

- `delete_rollout_payloads(object_store, object_keys)`：**段级**——对象镜像 + 逐 key 同构热段；
- `delete_thread_rollout_payloads(object_store, object_keys, thread_id)`：**thread 级**——上面的
  + `sweep_thread_hot_segments` 按 `<thread_id>` 目录清扫。

三条入口全部改走统一助手；`thread_deletion` 的删除块从"`if rollout_keys:` 才删"改成
"**只要有 rollout 行就必须能删**"，删除成功即**无条件**落 tombstone（未封存段也被标 deleted）。

**为什么 thread 级用"目录清扫"而不是"按 `(thread_id, ordinal_start, segment_id)` 现算路径"**：
现算路径还需要 thread 创建时间来定日期目录，而 `conversation_threads.created_at` 与记录器写入时
**传入的** `thread_created_at` 不保证同日（`runner` 在 thread 行缺失时取 `clock.now()`），
算出来的目录会指到别处；按 `<thread_id>` 目录反查与日期无关，且顺带覆盖 kodo 本地缓存与
"没有 manifest 行的孤儿热文件"。`delete_hot_thread_files()` 只删 `parse_segment_filename` 认得的
段文件，不碰 `.tmp`/回滚预留命名，也不清理目录（避免与其它写者竞态）。

### 3.3 新发现 15：root 级热删除能力

- 新增 `HotSegmentRootDeleter` 协议 + `LocalHotSegmentCache`（可挂到任意 store 上）；
- `LocalRolloutObjectStore` 实现 `delete_hot_thread`；
- `QiniuKodoRolloutObjectStore` 新增可选 `hot_cache` 参数并实现两个热删除方法；
  **未挂载时显式抛错**（不是静默 no-op）——静默 no-op 正是这条缺陷的病根；
- `build_rollout_object_store` 对 kodo 分支传入 `LocalHotSegmentCache(root=conversation_rollout_root)`，
  于是 **worker / app / CLI 三个装配点产出的 store 都能删自己写过的热段**。

### 3.4 新发现 4①：序数范围重叠（**选择"改取本地热段真实最大 ordinal"**）

**方案对比**（题面给了两个选项）：

- **append 时持久化 `ordinal_end`**：需要每条 append 都写一次 DB（或按批写），把 rollout 旁路
  拉进 turn 关键路径，且与"懒创建 + 逐行 flush"的设计冲突；
- **改取本地热段的真实最大 ordinal**（本轮选择）：`resolve_resume` 新建段前先扫该 thread 的
  全部分片目录（`iter_thread_dirs`），取 `ordinal_start` 最大文件的**尾部行** ordinal，
  与 manifest 的 `max(COALESCE(ordinal_end, ordinal_start))` **取大**。
  代价是每次 `open_turn` 多一次目录列举 + 一次尾部读（≤64KB），与 Phase 1 退路
  `_next_ordinal_start` 同量级；跨节点恢复时本地无文件 → 退回 manifest 值，语义不变。

**承重验证**：临时把 `local_max_ordinal=None`（关掉扫描）→
`test_new_segment_avoids_unsealed_segment_ordinal_range` 变红：

```
E  AssertionError: 新段起点必须是「本地热段真实最大 ordinal + 1」，否则两段序号范围重叠
E  assert 1 == 2
```

撤销后通过。测试同时固定了"DB 侧盲区"（`next_ordinal_start == 1`）与"两段文件里 ordinal 不重复"。

### 3.5 新发现 4②：改名失败不再谎称"登记失败"

`register_open` 的结构改为：**DB 事务先提交**（标废旧行 + 插新行），**再**改名；改名失败走**补偿**：

1. 把刚插入的行标 `deleted`（manifest 与实际"没有可用段"的事实一致）；
2. 尽力 `unlink` 改名源文件（它的旧行已是 tombstone，留着就是不可发现的残留），
   连删除都失败时按 **ERROR 记日志 + 打指标**，路径信息进日志便于人工清理；
3. 返回**新状态** `RegistrationStatus.rename_failed`（与"没有 manifest 行"的 `failed` 区分），
   调用方同样停止本 turn 的记录，但日志/指标能区分两种故障。

补偿本身失败也不抛错：此时留下 `open` 行 + 缺失的本地文件，正好被 reconcile 的
`open_local_missing` 检出（可发现、可收敛）。

**为什么不是"改名移到事务之前"**：那样在"失租 worker 复用他人段"的场景里会先把**当前持有者**
的热段改名走，再因唯一约束失败——把别人的段弄坏。DB 先提交 + 补偿的方案在该场景下
`mark_deleted` 的租约守卫直接拒绝标废、插入撞唯一约束 → 不做任何文件操作。

### 3.6 新发现 5：降级收敛到单 turn

`_degraded` 语义改为"**本 turn** 已降级"，新增 `degraded_reason` 与 `_mark_degraded`/`_clear_degraded`：

- `open_turn` 开头清除（新 turn 重新尝试）；
- **turn 内不清除**（即使后续登记成功）：被跳过的行可能是必须首行的 `thread_meta`，
  中途续写会留下无法解释的序号空洞、破坏"thread_meta 是段内首行"不变式（这一点比题面给的
  "登记成功后清除"更保守，理由写在代码注释与测试 docstring 里）。

### 3.7 新发现 11：预算耗尽时不给可执行 offset

`_attach_read_hint` 在 `delivered_lines == 0`（预算连一行都放不下）时不再给 `line_offset`
（`consumed + 0` 正是请求的同一 offset），改为：

```json
{"hint": {"tool": "memory.read", "memory_id": "...", "retryable": false,
          "note": "本轮记忆工具内容预算已耗尽，请勿重复读取同一位置；请基于已有信息作答"},
 "reason": "memory_tool_budget_exhausted", "truncated": true}
```

仍是合法 JSON、`truncated=true`；顺带删掉 `_budget_exhausted_stub()` 那个永不使用的形参。

### 3.8 新发现 17：`resolve_resume` 去掉 fence 参数 + 在写路径补守卫（**选择"删掉并说明"**）

理由：`resolve_resume` **只读**、不改任何状态，"fence 一个只读函数"没有语义；它此前接收
`fence` 却从不使用，只会让读者误以为恢复路径也受 fencing 保护。真正的洞在**标废写路径**上，
因此本轮：

- `manifests_repo.mark_deleted(..., fence=...)`：先 `EXISTS(lease)` 校验，失租 → 返回 `False`
  且**不清消息指针**（不再"tombstone 别人的段还顺手清空其指针"）；
- `sealer.register_open(..., fence=...)` 把 fence 传给标废旧行；
  `sealer.discard_open(segment_id, fence=...)` 同理；recorder 两处都传 `self._fence`；
- `resolve_resume` 的 `fence` 形参删除，docstring 写明"有意不 fence + 守卫在哪"。

**保留行为**：`open_turn` 的 fence 仍然照旧用于 `seal()`；`test_seal_requires_fence_owner`
（失租者仍能登记 open 段、但不得封存）语义未变。

### 3.9 新发现 10（主控追加）：prime 的 summary 与目录共用同一预算

`trim_prime_to_budget` 重写为"**先扣 summary、再按剩余预算裁目录**"：

1. `summary` 计入 `conversation_memory_token_budget`；超预算时按 token **二分截断**
   （候选串把省略标记一起计数，避免"多出 1 token"）并置 `summary_truncated=true`；
   预算小到只剩几个字符时返回空串（不注入被切碎的摘要）；
2. 目录用**剩余**预算裁前缀，置 `index_entries_truncated=true`；
3. 目录为空/缺失时**不再提前 return**（旧实现正是在这里漏掉了 summary 的预算）；
4. `budget_tokens <= 0` 或没有 token 计数器 → 原样返回（同一对象），flag 关闭路径逐字不变；
5. 节点侧 `_recall_via_prime` 的降级标记门限统一改成 `_is_prime_truncated(prime)`
   （此前只看 `index_entries_truncated`，纯 summary 截断会**不发标记**——这是本轮顺带发现的一个
   同类缺口），日志补 `summary_truncated=%s`。

**没有新增降级标记**（复用既有 `memory_prime_degraded`），因此无需再动 Literal；元测试 A 顺带
固定了这一点（`memory_prime_degraded` 仍在 Literal 里且仍被生产）。

补齐的断言：`summary` 单独超预算 → 目录裁到 0 条 + 两个标记都在 + 总量 ≤ 预算；
summary 与目录共用预算（总量 ≤ 预算）；无目录时 summary 也被裁；节点侧纯 summary 截断也发
`memory_prime_degraded`；flag 关闭时视图里不出现 `prime` 键且裁剪零副作用。

---

## 4. 发现的其他同类问题（本类缺陷的"下一批候选"）

### 4.1 `DegradedFlag` 里有两个**永不产生**的死值（新发现，已白名单登记）

`retrieval_partial` / `retrieval_unavailable` 在 `DegradedFlag` 里，但 `backend/conversation/`
**没有任何生产者**（检索节点完全不产出降级标记）；唯一出现处是
`tests/unit/test_rollout_node_wiring.py` 的夹具。已在元测试的 `NEVER_PRODUCED` 里写明原因
（冻结契约预留 + 当前无生产者），并做**双向精确**校验：将来补上生产者必须删掉白名单项。

### 4.2 `graph_failed` 只进 rollout 审计记录（已在元测试显式登记）

`runner._record_turn_failed` 写的 `graph_failed` 走 `record_rollout` 的 payload，
`contracts/rollout.py` 的该字段是开放的 `list[str]`，不经过 SSE 封闭 Literal——因此在
`ROLLOUT_ONLY` 里登记，并注明"这不是遗漏"。

### 4.3 其它观察

- **`_budget_exhausted_stub(rendered)` 的形参从未使用**（已顺手删除）：一个"永不使用形参"在
  这类"最小形态"函数里会诱使后来者把原结果塞回去，从而破坏"有界"不变量。
- **reconcile-orphans 从不删对象**：这是有意的授权边界（`repair` 的 docstring 明确写了
  "孤儿对象删除留给人工"），但它确实是"会标记 deleted 的入口"，因此在 CLI 白名单 +
  一个集成用例里固定住该边界，避免未来被误当成"漏了一半"或被悄悄改成删对象。
- **`HotSegmentDeleter` 语义收紧**：`delete_hot_segment` 返回 `False`（能力缺失）现在**算失败**，
  删除入口据此拒绝 tombstone；`delete_hot_thread_segments` 用 `None`（能力缺失）与 `0`
  （有能力且确实没残留）严格区分——两者混用会重新引入"没删数据却宣称删了"。

---

## 5. 改动文件清单

**生产代码（14 个，全部在 `backend/conversation/**`）**

| 文件 | 改动 |
|---|---|
| `contracts/api.py` | `DegradedFlag` 补 3 个值 + 注释指向元测试 |
| `rollout/deletion.py` | **新增**：段级/thread 级统一删除助手 + 报告对象 |
| `rollout/object_store.py` | `delete_hot_thread_files`、`HotSegmentRootDeleter`、`LocalHotSegmentCache`、`delete_hot_thread_segments`；Local/Fake 实现按 thread 清扫 |
| `rollout/file_naming.py` | `iter_thread_dirs`（跨分片）、`max_local_ordinal`（真实最大 ordinal） |
| `rollout/sealer.py` | 删 `resolve_resume` 的 fence 形参、本地 ordinal 取大、`rename_failed` 状态 + 补偿、`register_open`/`discard_open` 带 fence |
| `rollout/recorder.py` | 降级收敛到单 turn（`_mark_degraded`/`_clear_degraded`/`degraded_reason`）、传 `local_max_ordinal`、两处传 fence |
| `rollout/factory.py` | kodo 分支挂 `LocalHotSegmentCache` |
| `rollout/qiniu_kodo.py` | 可选 `hot_cache` + 两个热删除方法（缺能力显式报错） |
| `services/thread_deletion.py` | 改走 thread 级统一助手、未封存段也删、无条件 tombstone |
| `cli/rollout.py` | `delete-thread-rollouts` / `retention-scan` 改走统一助手；retention 缺 key 显式失败 |
| `persistence/rollout_manifests.py` | `fence_holds()` + `mark_deleted(fence=...)` |
| `graph/nodes/memory_tool.py` | 预算耗尽时不给可执行 offset；stub 去掉无用形参 |
| `services/context_service.py` | `trim_prime_to_budget` 让 summary 与目录共用预算 |
| `graph/nodes/memory.py` | 降级标记门限统一为 `_is_prime_truncated`；日志补 summary 事实 |
| `worker/job_worker.py` | 订正"为 None 时跳过对象删除"的过期注释 |

**测试（8 个：4 新增 / 4 修改）**

- 新增 `tests/unit/test_degraded_flag_enum_meta.py`（元测试 A）
- 新增 `tests/unit/test_rollout_deletion_paths_meta.py`（元测试 B）
- 新增 `tests/integration/test_rollout_deletion_paths.py`（真值表 + 参数化入口）
- 新增 `backend/conversation/rollout/deletion.py` 的单元覆盖落在上面两个元测试与集成测试里
- 修改 `tests/conversation/test_rollout_seg_retry.py`（+3 个真库用例：序号重叠 / 改名补偿 + 单 turn 降级 / fencing）
- 修改 `tests/unit/test_rollout_recorder.py`（+2 个单 turn 降级用例）
- 修改 `tests/unit/test_rollout_assembly_hot_cache.py`（kodo 热缓存能力新语义）
- 修改 `tests/unit/test_memory_tool_node.py`（预算耗尽 hint 语义）
- 修改 `tests/unit/test_prime_injection_budget.py`（summary 共用预算 + flag 关闭零副作用）

---

## 6. 门禁实跑（原始输出尾部）

```console
$ uv run ruff check backend tests
All checks passed!

$ uv run ruff format --check backend tests
519 files already formatted

$ uv run mypy backend
Success: no issues found in 310 source files

$ uv run pytest tests/unit tests/test_mineru_ocr_*.py -q -p no:randomly
1082 passed, 385 warnings in 16.87s

$ uv run pytest tests/contract -q
5 passed, 24 warnings in 2.00s

# 对话域集成（真实 PG + 真实文件系统，串行；scripts/ci-local.sh 的 *_test 环境变量）
$ DATABASE_URL=...memory_test CONVERSATION_DATABASE_URL=...conversation_test \
  uv run pytest tests/integration tests/failure_recovery tests/conversation tests/community tests/study -q
486 passed, 3 skipped, 605 warnings in 46.14s

# rollout 子集（我改动最密的部分）
$ uv run pytest tests/conversation/test_rollout_seg_retry.py tests/conversation/test_rollout_seal_read.py \
    tests/integration/test_rollout_deletion_paths.py tests/integration/test_rollout_hot_cache_deletion.py -q
37 passed, 7 warnings in 4.87s
```

- **契约快照未重生成**：本轮没有改路由、请求/响应模型或 SSE payload 字段形状（`DegradedFlag`
  的取值集合不在 OpenAPI 快照里，`tests/contract` 5 passed 即为证据）。
- **承重验证（两处）**：
  - 关掉本地 ordinal 扫描 → `test_new_segment_avoids_unsealed_segment_ordinal_range` 报
    `assert 1 == 2`；撤销后通过；
  - 去掉 thread 清扫 → 真值表 `object_key=无(未封存)+hot=有` 报
    `thread_deletion：热缓存段必须被物理删除`；撤销后通过。

---

## 7. 未做/无法做

- **未提交 git**（题面要求）。
- **改动 `memory-rebuild-deviations.md`**：题面禁止，因此本轮新登记的事实（死枚举
  `retrieval_partial`/`retrieval_unavailable`、`rename_failed` 状态语义、kodo 热缓存能力）
  只写在代码注释与本报告里，**建议由主控统一汇总登记**。
- **`append` 时持久化 `ordinal_end`** 的方案未采纳（理由见 §3.4），因此跨节点恢复仍依赖
  manifest 值——那条路径本来就没有本地文件，语义不变。
