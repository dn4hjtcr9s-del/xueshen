# 报告：多处投影站点的全量枚举型元测试 + review-2 七项修复

- **分支/工作树**：`codex/memory-rebuild-implementation` @ `.local/worktrees/memory-rebuild-implementation`
- **基线**：`685bfc6`（复审报告 `REVIEW-2-verification-685bfc6.md` 的复审对象）
- **范围**：复审新发现 **1（Critical）/ 3 / 7 / 9 / 12 / 13 / 14 / 16** + **硬性交付：多处投影站点枚举型元测试**
- **纪律**：未提交 git；未改 `memory-rebuild-deviations.md`；未改 `backend/conversation/**`、`runner.py`、`summary.py`、
  `memory_tools.py`、`contracts/**`（`consolidation.py` 只动了标题投影规则，与另一位负责人的 `operation_id` 改动不冲突）

---

## 0. 结论

| 项 | 结论 |
|---|---|
| 新发现 1（Critical，多节点提交杀光 KG 映射） | **已修**：改一次调用 + 集合排除；断言升级为"每个计划内节点 active=True"；用**变异测试**证明新断言能抓住旧的循环实现 |
| 新发现 3（`commit_started_at` 残留） | **已修**：`_clear_commit_started` 补传 `fencing_operation_id` |
| 新发现 7（restore / index.md 仍用 `topic_title`） | **已修且补全**：新增唯一权威实现 `index_projection_from_document` + `projected_title`；又发现**第五、第六个标题站点**（`consolidation._mastery_view` / `_learner_view`）并一并归口；`search_text` 也改用投影标题 |
| 新发现 9（迁移回填只覆盖本次升级） | **已修**：`skipped_already_v2` 分支同样刷新投影（= 全量回填入口），新增 `refreshed_already_v2` 计数；代价见 §3.4 |
| 新发现 12（整批全灭仍报 succeeded） | **已修且今天就生效**：抛 `BatchAllMembersFailedError`（`OperationDeadLetterError` 子类，`retryable=False`）→ 批次落 `dead_letter`，**不需要改 `manager.py` / `retry.py`** |
| 新发现 13（失败成员无尝试计数、可无限重试） | **已修**：`release_batch_member` 复用既有 `attempt_count` 列 +1；达到该行自己的 `max_attempts`（证据类 = 4）时不再释放、转 `dead_letter` 交人工审（**无新迁移**） |
| 新发现 14（取消成员被误报"无归属"） | **已修**：区分"行还在（已取消/已释放/已终态）→ 按设计跳过（进 `batch_processed`，中性措辞）"与"行不存在 → 真·数据不一致，保留警告并点明" |
| 新发现 16（死路由） | **已删** `batch.route_after_summary_finalize`，单测改为断言"它不存在 + runner 的那份才是生效路由" |
| **硬性交付：枚举型元测试** | **已交付** `tests/unit/test_projection_sites_meta.py`：AST 自动枚举 **27 个站点**（+3 条显式豁免 +2 条手动登记）、4 类断言（字段清单 / 取值来源 / 跨站点一致 / 结构委托），**4 个变异实验全部被抓** |

新增/修改文件（只列我名下的）：

| 文件 | 改动 |
|---|---|
| `backend/memory/services/memory_service.py` | 新增 `LEARNER_PROFILE_TITLE` / `projected_title` / `index_projection_from_document`；三条写入路径共用；`_index_entry_from_projection` 标题优先取投影行；`_sync_graph_links` 一次调用+集合排除+历史行元数据回退；`_clear_commit_started` 补 fencing |
| `backend/memory/persistence/graph_states.py` | `deactivate_graph_links(except_node_ids=…)`（集合语义）+ 新增 `list_links_for_memory` |
| `backend/memory/persistence/operations.py` | `release_batch_member` 返回 `BatchMemberReleaseOutcome`（释放/死信/未命中 + 尝试计数）；新增 `list_operations_by_ids` |
| `backend/memory/graph/batch.py` | `BatchAllMembersFailedError`（批次级失败信号）；`load_batch_members` 区分"不可处理"与"不存在"；`_ReleaseSummary`；删除死路由 |
| `backend/memory/graph/maintenance.py` | 迁移的 `skipped_already_v2` 分支回填投影 + `refreshed_already_v2` |
| `backend/memory/graph/consolidation.py` | `_mastery_view` / `_learner_view` 的标题改走 `projected_title` |
| `tests/unit/test_projection_sites_meta.py` | **新增**（元测试，40 个用例） |
| `tests/unit/test_memory_batch_graph.py` | 同步删掉死路由断言 + 新增 4 条批次级失败信号用例 |
| `tests/integration/test_mastery_kg_links_and_projection.py` | `active=True` 断言 + 2 条多节点真值表用例 + 重命名→删除→恢复 + index.md 一致性 |
| `tests/integration/test_memory_schema_migration.py` | 2 条"已是 v2 也回填投影"用例 |
| `tests/integration/test_memory_batch_worker_e2e.py` | 3 条真实 Worker 用例（整批全灭→dead_letter、达上限→死信、取消 vs 不存在） |

---

## 1. 门禁实测（原始输出尾部）

```
$ uv run ruff check backend/memory/services/memory_service.py backend/memory/persistence/graph_states.py \
    backend/memory/persistence/operations.py backend/memory/storage/markdown_schema.py \
    backend/memory/graph/maintenance.py backend/memory/graph/consolidation.py backend/memory/graph/batch.py \
    tests/unit/test_projection_sites_meta.py tests/unit/test_memory_batch_graph.py \
    tests/integration/test_mastery_kg_links_and_projection.py tests/integration/test_memory_schema_migration.py \
    tests/integration/test_memory_batch_worker_e2e.py
All checks passed!

$ uv run ruff format --check backend/memory tests/unit/test_projection_sites_meta.py ...（同上文件集）
102 files already formatted

$ uv run mypy backend
Success: no issues found in 310 source files

$ uv run pytest tests/unit tests/test_mineru_ocr_*.py -q          # 首轮（另一位负责人的文件正在编辑中）
3 failed, 1073 passed, 385 warnings in 18.00s

$ uv run pytest tests/unit -q                                     # 最终轮（对方文件落定后）
1064 passed, 385 warnings in 16.92s
  ↑ 首轮那 3 个失败全部在**另一位负责人当时正在写的** tests/unit/test_rollout_deletion_paths_meta.py
    （先 8 个 ruff 错、后 3 个，随后其单测通红），与本轮改动无关；对方落定后全绿。

$ uv run pytest tests/unit/test_projection_sites_meta.py -q
40 passed in 1.11s

$ DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test" \
  uv run pytest tests/integration/test_mastery_kg_links_and_projection.py -q
10 passed, 5 warnings in 0.89s

$ DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test" \
  uv run pytest tests/integration/test_memory_schema_migration.py -q
7 passed, 5 warnings in 1.18s

$ DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test" \
  uv run pytest tests/integration/test_memory_batch_worker_e2e.py -q
5 passed, 5 warnings in 0.73s

$ DATABASE_URL=...memory_test uv run pytest tests/integration/test_batch_cancel_release.py \
    tests/integration/test_memory_batch_graph.py tests/integration/test_scheduler_pending_batch.py \
    tests/integration/test_evidence_batch_submission.py -q
29 passed, 33 warnings in 2.57s

$ （完整集成 stage，注入 memory/auth/conversation/community/study 五条 *_test URL）
  uv run pytest tests/integration tests/failure_recovery tests/conversation tests/community -q
  # 首轮：1 failed, 427 passed, 3 skipped（唯一失败是对方正在改的 test_rollout_deletion_paths.py）
  # 最终轮：
431 passed, 3 skipped, 396 warnings in 46.87s
```

全仓 `ruff check backend tests` 在我完成时仍有 **3 个 E501**，全部落在 `tests/unit/test_rollout_deletion_paths_meta.py`
（另一位负责人正在编辑的文件）；`ruff format --check` 报的 3 个未格式化文件同样是他的
（`memory_tool.py` / `test_rollout_assembly_hot_cache.py` / `test_rollout_deletion_paths_meta.py`）。
**我名下的 12 个文件 ruff / format / mypy 全绿。**

---

## 2. 新发现 1（Critical）：多节点提交杀光 KG 映射

### 2.1 修法

`_sync_graph_links` 改为**一次** `UPDATE ... WHERE node_id <> ALL(CAST(:ids AS text[]))`（集合排除），
排除集合是**本次真正写成功的节点**（`upserted`）。顺带修掉同一段里的第二个坑：method/confidence 的
回退来源从"上一版活动的行"扩大到"该节点的任意历史行"，于是**曾经映射过、当前 inactive/旧版本**的
节点也能被本次提交重新激活（旧代码会让它被集合排除挡在外面、永远停在 inactive）。

### 2.2 真值表（情况 1：本次提交携带 `node_ids`）

| # | 计划内节点 N | 表内已有行 | 本轮给 `mapping_method` | 结果 |
|---|---|---|---|---|
| 1 | 是 | 上一版活动的行 | 是 | upsert(active=true, v=new, 本轮 method/conf) |
| 2 | 是 | 上一版活动的行 | 否 | upsert(active=true, v=new, 沿用前序行 method/conf) |
| 3 | 是 | 只有 inactive / 旧版本行 | 是 | upsert(active=true, v=new, 本轮 method) |
| 4 | 是 | 只有 inactive / 旧版本行 | 否 | upsert(active=true, v=new, 沿用**该历史行** method/conf) ← 本次新增能力 |
| 5 | 是 | 无行 | 是 | INSERT(active=true, v=new) |
| 6 | 是 | 无行 | 否 | 不落库（`mapping_method` NOT NULL + CHECK 白名单，不能编造来源；表里也没有行会被误杀） |
| 7 | 否 | 有 active 行 | — | active=false ← "映射被本次集合改写"（§16.4） |
| 8 | 否 | 已 inactive | — | 不变（仍 inactive） |

情况 2（`node_ids` 为空）：把 `active=true AND memory_version=v-1` 的行全部 upsert 到 new，**不 deactivate 任何行**（逐字不变）。
情况 3（两者皆无）：不触碰 `memory_graph_links`。

**为什么不采用 brief 里提到的"排除集合 = planned ∪ previous"**：

- `previous ⊇ 该记忆当前所有 active 行`，把它并入排除集合等于"drop 一个节点永远不生效"，与 §16.4
  的"节点集合以本次提交为准"以及既有验收用例
  `test_explicit_empty_node_list_is_the_only_way_to_drop_mappings`（断言 `other_node` 必须变成 inactive）直接冲突；
- 被排除却留在表里的行是 `active=true` 但 `memory_version=v_old`：`_dual_write_kg` 要求
  `active AND memory_version=:v`、`list_current_active_links` 要求 `l.memory_version=d.active_version`，
  两个读者都看不到它——它既不能自愈（下一次提交用 `v-1` 也找不到），又会污染
  `list_active_links_for_memory(active_version-1)` 的判断。

排除集合取 `upserted` 后，"计划内节点"与"保持 active 的节点"在**所有有行可继承**的情况下完全重合
（真值表第 1–5 行），只剩第 6 行（无行、无来源）不落库——那一行也没有任何已存在的映射会被误伤。

### 2.3 测试与变异验证

- 断言升级：`test_explicit_empty_node_list_is_the_only_way_to_drop_mappings` 现在对**每个**计划内节点断言
  `active is True` 且 `memory_version == 2`（旧断言只比行集合，两条 inactive 行同样满足）；
- 新增 `test_planned_node_without_new_method_is_reactivated`（真值表第 4 行）、
  `test_all_planned_nodes_stay_active_without_graph_metadata`（第 6/7 行边界）；
- **变异实验**：把实现改回 `for kept in upserted: deactivate(except_node_ids=[kept])` →
  `AssertionError: 计划内节点 n7711 被置为 inactive（多节点提交把所有映射杀掉了）`
  与 `AssertionError: 计划内节点必须重新激活`；恢复后 10 passed。

---

## 3. 其余各项修法与取舍

### 3.1 新发现 3：`_clear_commit_started` 补传 `fencing_operation_id`

`finally` 里的 clear 现在与 mark 打在同一行（`cas_operation_id`）。漏传时 CAS 落在从未被 claim 的成员行上恒 0 行，
批次行 `commit_started_at` 残留 → 取消仲裁误判"正在提交"。标记语义现在成对成立。

### 3.2 新发现 7：标题投影归口（并补上第五、第六个站点）

新增两个权威函数：

- `projected_title(doc)`：v2 取 frontmatter `name`（空则回退），v1 取 `topic_title`，learner 无标题时回退
  `LEARNER_PROFILE_TITLE`（"学习者档案"）；
- `index_projection_from_document(doc, *, related_topic_keys=None)`：注册表投影载荷（title / summary /
  keywords / aliases / related_topic_keys / search_text）的**唯一实现**。

**提交（`_build_new_content` 两支）、恢复（`restore`）、迁移刷新（`refresh_index_projection`）三条写入路径全部改调它**
（提交路径仍显式传 `_links_of_rendered(content)`，保持 I-11② 的"从新渲染正文现算"）；`rebuild_index` 侧的
`_index_entry_from_projection` 改为**优先取 PG 投影行的 title**，缺失时才回退文档行（保证历史数据可重建）。

顺手收掉的同类站点（复审只点了 restore/index.md 两处）：

- `consolidation._mastery_view`：自己写 `mastery.name or mastery.topic_title`（无 v2 判定、无回退）——第五个站点；
- `consolidation._learner_view`：根本没有 `name` 键，链接命名空间（`_govern_dangling_links` 读
  `doc.get("name") or doc.get("topic_title")`）看不到 learner 的 v2 名称——第六个站点；现在两个视图键集合对齐；
- `search_text` 的首段从 `topic_title` 改成投影标题（否则 v2 文档的 `name` 进不了 trigram 召回）。

**关于 `description`**：注册表的 `summary`（= index.md 的 `- description`）刻意仍取**正文概述**而不是
frontmatter `description`，这是 DEV-023 已登记的取舍。元测试把它写成两类来源（`BODY_SUMMARY` vs
`DESCRIPTION`）并逐站点断言，谁要改这个决定必须同时改真值表——不会被"顺手改一处"绕过。

### 3.3 新发现 12：整批全灭的批次级失败信号

`finalize_batch_result` 在 `mutations == [] and review_candidate_ids == [] and failed` 时抛
`BatchAllMembersFailedError`（`OperationDeadLetterError` 子类，`retryable = False`，`code = "BATCH_ALL_MEMBERS_FAILED"`）。

**为什么用异常而不是只写 `state["errors"]`（取舍）**：

- `normalize_result` 目前只识别 `LLM_BUDGET_EXHAUSTED` 一个 code（`manager.py` 不在我名下）。只写一个结构化
  code 不改变任何状态，等于"登记了但没生效"——那正是复审批评的"只做一半"；
- 伪造 `LLM_BUDGET_EXHAUSTED` 会把"预算耗尽"这个真实语义污染掉（运维据此排查会走错方向），因此不采用；
- 继承 `OperationDeadLetterError` 后 `retry.py::classify_failure`（非 retryable 的 `MemoryError` → `DEAD_LETTER`）
  **无需任何改动**就把批次落成 `dead_letter`，且不会进 retry 循环；
- 代价：异常路径下 `MemoryOperationResult.result` 为 `None`，逐成员的 `batch.processed` 结构化诊断只在图
  state / checkpoint 里。因此异常消息里带上了人数与首条失败摘要（`public_error.message`，上限 500 字符），
  实测 `code=BATCH_ALL_MEMBERS_FAILED`、`message` 含 `2/2` 与 `member_exception@extract_candidates`。
- **仍差的一步（需主控决定）**：`OPERATION_*` 之外的新 code `BATCH_ALL_MEMBERS_FAILED` 未进
  `contracts/errors.py::ERROR_CODES`（`contracts/**` 不在我名下，且该集合当前无人校验）。若希望公开错误码
  白名单自洽，请把它登记进去；若希望改成 `state["errors"]` 路线，请让 `manager.py` 认这个 code 并删掉这个异常
  （两者只应存在其一）。

单成员失败仍不拖垮整批（§5.8）：只有当 `mutations` 与 `review_candidate_ids` **双空**且 `failed` 非空时才升级；
全是审核候选的批次仍交回 `normalize_result` 判 `needs_review`（已加单测固定该边界）。

### 3.4 新发现 9：迁移回填的"全量入口"

**选法**：让 `migrate_markdown_schema_v2` 在 `skipped_already_v2` 分支也调用 `refresh_index_projection`（非 dry-run），
并新增计数 `refreshed_already_v2`。

**为什么不做成新的维护类型**：新增 kind 要改 `MaintenanceCommand` 的 Literal 与路由表（`contracts/**` 不在我名下），
而现有任务本身就该是"把投影补齐"的入口（0008 的 docstring 就是这么承诺的）。

**代价（诚实说明）**：每次运行都会对**已是 v2 的活动文档**重算并 upsert 一次投影、标一次 index dirty（幂等、不写新版本、
不改正文）。批量与 cursor 有界，且该任务受 `memory_schema_v2_migration_enabled` 门控、每天最多建一个 run
（`scheduler.py:170` 04:15），因此代价可控；换来的是"**无论何时**跑迁移都能补齐投影"这一确定性。若运维把该 flag
长期开着，稳态下每天会多一轮投影 upsert + 一次 index.md 重建——如果主控认为这不可接受，建议的后续改进是
"先比对投影行，只在缺失/不一致时写"（本次未做，避免再加一层 API）。

测试：`test_second_run_backfills_projection_of_already_v2_documents`（投影行缺失 → 跑迁移 → 行补齐、版本不动、
旧版本字节不变、index dirty 标上）与 `test_projection_backfill_is_idempotent_across_runs`。
**变异验证**：把该分支改回"直接 continue" → 两条用例立刻失败。

### 3.5 新发现 13：失败成员的尝试计数与终态出口

`release_batch_member` 在**同一条** UPDATE 里用 CTE 完成：`attempt_count = attempt_count + 1`，并且当
`attempt_count + 1 >= max_attempts`（复用该行自己的上限；证据类 operation 是 P2 → 4）时改为
`status = 'dead_letter'` + 写 `public_error` + `completed_at`，**不释放**（保留 `batch_operation_id` 以便追溯）。
返回 `BatchMemberReleaseOutcome(status, attempt_count)`；`batch.py` 据此分别输出"已释放回证据池"与
"已达 max_attempts 转 dead_letter"两条 warning。

- **没有新迁移**：`memory_operations.attempt_count` / `max_attempts` 是 0001 就有的列；成员从未被 claim，
  所以此前这个计数对成员永远是 0；
- 没有改变"宁可重试也不永久丢用户数据"的默认：前 3 次仍然释放回池子（`next_run_at = now()`），第 4 次才进人工审核，
  与任务级 `max_attempts` 同一把尺子；
- 测试：`test_repeated_failed_member_escalates_to_dead_letter_at_max_attempts`（真实 Worker：成员落 dead_letter、
  `attempt_count == max_attempts`、保留归属、不再出现在 `list_pending_batch_members`），
  以及整批全灭用例里断言 `attempt_count == 1`。

### 3.6 新发现 14：取消/已释放 vs 真·不存在

`load_batch_members` 现在回查一次"声明了却没返回"的 id（新增 `list_operations_by_ids`）：

- **行还在**（cancelled / 已释放回池子 / 已终态）→ 记进 `batch_processed`（`outcome="skipped_not_processable"`，
  中性措辞"成员状态为 X（已取消/已释放/已终态），按设计跳过"）+ debug 日志，**不进 warnings**；
- **行不存在** → 保留警告，措辞改为"…在 `memory_operations` 中不存在（归属丢失/数据不一致，需人工核查）"。

测试：`test_load_batch_members_separates_cancelled_from_missing`（构造一个 cancelled 成员 + 一个不存在的 id，
断言前者不进 warnings 且出现在 `batch_processed`，后者必须进 warnings 且点明不一致）。

### 3.7 新发现 16：删除死路由

`graph/batch.py::route_after_summary_finalize` 已删除；`tests/unit/test_memory_batch_graph.py` 改为
`assert not hasattr(batch_module, "route_after_summary_finalize")` + 断言真正生效的
`runner._route_after_finalize_summary_result` 行为（批量回成员循环、单条 `continue`）。

---

## 4. 硬性交付：多处投影站点的枚举型元测试

文件：`tests/unit/test_projection_sites_meta.py`（40 个用例，纯单元、无数据库、无需 mock 一个真实 PG）。

### 4.1 它"枚举"出了哪些站点（AST 自动提取，27 个）

发现规则（四条互补，任一命中即必须是已登记站点）：

1. **锚点**：引用了投影管线符号（`index_projection_from_document` / `projected_title` / `_v2_front_matter` /
   `_render_index_block` / `_render_index_v2` / `_render_index_item` / `render_index` / `_upsert_index_entry` /
   `_index_projection` / `_index_entry_from_projection` / `_index_entry_from_block` / `_parse_index_blocks` /
   `render_mastery` / `render_learner`）；
2. **载荷字典**：出现含 ≥3 个投影字段拼写、或 ≥2 个"注册表核心键"的 dict 字面量；
3. **IndexEntry 构造**：`IndexEntry(...)` 带 ≥2 个 index 条目字段（解析侧站点）；
4. **SQL**：`text("...")` 里出现 `memory_index_entries`（落库/读回站点）。

提取到的 27 个站点（`kind`：payload=自己拼载荷；delegate=必须调权威实现；consumer=只分派；
read/construct=读回与解析）：

| kind | 站点 |
|---|---|
| payload | `schema._v2_front_matter`、`schema._render_index_block`、`ms.index_projection_from_document`、`ms._upsert_index_entry`、`consolidation._mastery_view`、`consolidation._learner_view`、`consolidation._govern_dangling_links` |
| delegate | `schema.render_mastery`、`schema.render_learner`、`schema._render_index_v2`、`schema._parse_index_blocks`、`maintenance._upgrade_to_schema_v2`、`ms._build_new_content`、`ms.restore`、`ms.refresh_index_projection` |
| consumer | `schema.render_index`、`schema.parse_index`、`ms.commit_plans`、`ms.rebuild_index`、`consolidation._load_user_documents` |
| read / construct | `schema._index_entry_from_block`、`schema._parse_index_item`、`ms._index_projection`、`ms._index_entry_from_projection`、`summary.resolve_existing_memories`、`index_entries.search_candidates`、`dangling_links.link_namespace` |

另有 **3 条显式豁免**（被规则命中但确实不写投影，逐条写理由）：`consolidate_user_memory`（结果统计字典）、
`_dual_write_kg`（KG 输入载荷）、`MemoryService.forget`（删除路径，归另一位负责人的元测试）；
**2 条手动登记**（规则够不着，附理由）：`consolidation._load_user_documents`（锚点 `_mastery_view` 是多模块同名符号）、
`dangling_links.link_namespace`（纯函数、无 SQL 无载荷字典）；
**4 条只读消费站点**（有意不纳入枚举，逐条写理由）：`memory_tools.build_search_sql` / `fetch_index_projection`、
`search_service.SearchService.search`、`context_service.assemble_context`。

### 4.2 四类断言

1. **字段清单（清单与实现必须同时改）**：`REGISTRY_PAYLOAD_KEYS` / `FRONTMATTER_KEYS` / `INDEX_BLOCK_KEYS` /
   `PG_PROJECTION_COLUMNS` / `PG_INDEX_READ_COLUMNS` 是显式集合；对每个 payload/read/construct 站点，
   用 AST **提取它实际写/读的键集合**并与登记集合做**双向相等**断言。PG 侧另有 SQL 解析断言：INSERT 列、
   `UPDATE ... = EXCLUDED.*` 列、`SELECT` 列三处必须覆盖各自清单。
2. **取值来源真值表**：合成探针给每个权威来源一个**唯一哨兵值**（`PROBE-NAME-7f3a` ≠ `PROBE-TOPIC-TITLE-b21c`，
   aliases / keywords / links / description / overview 各自独立），逐站点断言"输出的某个键 == 登记的那个来源"。
   站点把 `name` 换回 `topic_title`、或漏掉 `aliases`，都会失败。
3. **跨站点一致**：提交 / 恢复 / 迁移刷新三条路径捕获到的 `index_data` 必须**逐键相等**（mastery + learner 两类），
   且三者的 `title` 都等于 frontmatter `name`；`rebuild_index` 渲染出的 index.md 解析回来必须与注册表投影相同。
4. **结构委托**：delegate/consumer 站点必须真的引用权威实现符号，且**体内不得再出现注册表载荷字典**
   （防止"又抄一份等价逻辑"）；`_V2_INDEX_FIELDS`（渲染/解析共用的字段顺序）必须与 `INDEX_BLOCK_KEYS` 相等。

### 4.3 为什么"新增一个投影字段 / 新增一个站点"一定会失败

| 未来的改动 | 触发的断言 |
|---|---|
| 给 `index_projection_from_document` 加一个键（如 `sources`） | `test_registry_payload_keys_match_the_single_implementation`（实际键集合 ≠ 清单）+ 各 delegate 站点的取值/键集合断言 → 必须同时改清单、`_upsert_index_entry` 的 SQL、index 渲染/解析、迁移评估 |
| 只给某**一个**站点加字段 | `test_site_key_inventory_is_exact`（该站点提取到的键集合 ≠ 登记）+ `test_every_site_accounts_for_every_projection_field`（其它站点既没承载也没写 `absent` 理由） |
| 把某个站点的取值来源换掉（name ↔ topic_title） | 取值来源真值表（`取值来源错了`）+ 跨站点一致断言 |
| 新写一个自己拼 index_data 的函数 | `test_no_undeclared_projection_site`（发现规则命中但未登记） |
| 新站点只引用权威实现（如再包一层） | 同上（锚点规则命中）；若确实不是投影站点，必须登记进 `DECLARED_NON_SITES` 并写理由 |
| 把投影逻辑从某个 delegate 站点里"抄回来" | `test_delegate_and_consumer_sites_call_the_pipeline` + `..._do_not_rebuild_the_registry_payload` |
| 给 PG 加列但只改写入不改读取（或反之） | `test_pg_projection_columns_are_written_and_read_consistently` |
| 站点改名/删除 | `test_required_sites_are_declared` / `test_every_declared_site_exists`（brief 要求的最小覆盖集是显式常量） |

### 4.4 元测试自身的变异验证（4/4 被抓）

| 变异 | 结果 |
|---|---|
| `_index_entry_from_projection` 的 title 换回 `doc["topic_title"]` | 2 failed（`..._prefers_the_projection_title`、`..._rebuild_index_matches_registry_projection`） |
| 给权威实现加 `"sources": [...]` | 5 failed（键集合清单 + 3 条写入路径 + 真值表） |
| 让 `restore` 自己拼一份投影 | 4 failed（委托/载荷/取值/跨站点一致） |
| 新增一个未登记的 `_rogue_projection()` | 1 failed（`test_no_undeclared_projection_site`） |

另外 `test_field_inventory_check_has_teeth` 与 `test_discovery_rule_has_teeth` 是"测测试自己"的自检：
清单对比确实会因"多一个键 / 少一个键 / 换来源"失败，发现规则确实在全仓扫出 >10 个站点。

---

## 5. 其它同类问题发现

1. **第五、第六个标题站点（已修）**：`consolidation._mastery_view` 自己写了一套"name 优先"规则却没有 v2 判定与
   learner 回退；`_learner_view` 干脆没有 `name` 键，导致 learner 的 v2 名称进不了链接命名空间。两者已归口到
   `projected_title`，并写进元测试真值表。
2. **`search_text` 的标题来源（已修）**：四个投影站点的 `search_text` 首段原先都取 `topic_title`（learner 是常量
   "学习者档案"），v2 文档改名后 trigram 召回想命中旧名。现在统一取投影标题，并由
   `test_search_text_starts_with_the_projected_title` 固定。
3. **`_upsert_index_entry` / `_index_projection` 的列清单（已纳入元测试）**：写入 6 列、读回 5 列（不含
   `search_text`）此前只靠人工对齐；现在 AST 解析 SQL 列清单双向校验。
4. **`release_batch_member` 的"errors-only 成员"仍是 `no_change` outcome（未修，建议登记）**：成员体
   `errors` 非空但既无 mutation 也无审核候选时，`record_batch_member` 判 `NO_CHANGE`（只有
   `member_exception` 的成员才被释放）。在**混合批次**里这类成员会被 `settle_batch_members(succeeded)`
   标记为 succeeded，等于"没写进去却算完成"。本次的最小修法是让"整批全灭"进死信（覆盖了最坏情形），
   但混合批次里的单条 errors-only 仍会静默完成——彻底修需要按成员回写终态（超出本轮授权范围）。
5. **`forget` 的投影删除是"无条件 DELETE 两列"**：删除路径只按 `(user_id, memory_id)` 删行，不涉及字段级投影，
   因此放进 `DECLARED_NON_SITES` 并把删除路径枚举留给另一位负责人的元测试。
6. **`memory_tools` / `search_service` / `context_service` 的只读消费站点**（有意不纳入）：它们只读 PG 投影列，
   字段漂移不会产生"投影不一致"，已在 `OUT_OF_SCOPE_READERS` 里逐条写明原因与替代覆盖。
7. **复审新发现 8（consolidation KG 审计锚）已在工作树中被另一位负责人修掉**
   （`consolidation.py:762` 现在传 `_batch_operation(state).operation_id`），我没有重复改动。

---

## 6. 需要主控做的登记/决策（我未改 `memory-rebuild-deviations.md`）

1. **登记**：注册表 `summary` ≠ frontmatter `description` 的现状现在有了可执行的"真值表 + 元测试"锚点
   （DEV-023 正文可补一句"由 `tests/unit/test_projection_sites_meta.py` 固定"）。
2. **决策**：`BATCH_ALL_MEMBERS_FAILED` 是否并入 `contracts/errors.py::ERROR_CODES`；若主控更希望走
   `state["errors"]` + `manager.py` 认 code 的路线，请把 `BatchAllMembersFailedError` 换成结构化 error
   （两者只应存在其一，见 §3.3）。
3. **登记（新）**：混合批次里"errors-only 成员"仍会被标 succeeded（§5.4），建议登记为待修项。
4. **登记（新）**：迁移回填的稳态代价（§3.4：flag 长期开启时每天多一轮投影 upsert + 一次 index 重建）。

## 7. 我**没有**做的事（诚实边界）

- 未验证 `memory_kg_dual_write_enabled` 全开的端到端推荐链路（只有 `tests/integration/test_kg_dual_write.py` 覆盖，
  本轮我改的是 `active`/`memory_version` 的写入语义，已由该文件 + 本文件 10 条用例覆盖）；flag 默认关闭，
  按约定"实现不等批准、启用必须等批准"。
- 未改 `manager.py`（新发现 12 的 code 映射）、未改 `contracts/**`（新错误码、维护 kind）。
- 未处理另一位负责人范围内的删除路径与 `DegradedFlag` 封闭枚举。
