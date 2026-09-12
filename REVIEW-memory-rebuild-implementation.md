# Review：`codex/memory-rebuild-implementation`（memory-rebuild Phase 0–7）

- **审查对象**：worktree `.local/worktrees/memory-rebuild-implementation`，HEAD `2721ef4`，merge-base `6f8a6db`（本地 `main`）
- **规模**：15 个提交、136 个文件、+28 365 / −287 行（生产代码 ≈11k，测试 ≈14k，文档 ≈2k）
- **审查方式**：5 条并行深挖（rollout 链路 / 记忆读路径 / 记忆写路径 / 跨切面装配）+ 本次会话逐点复核关键结论；4 个 Critical / Important 缺陷用真实 PostgreSQL（`memory_test` / `conversation_test`，端口 55432）**实测复现**
- **未改动任何文件**：所有探针写在 `/tmp` 或临时测试文件并在验证后删除；`git status` 已确认干净（本报告文件除外，属未跟踪交付物）

---

## 1. 门禁与验证结果（本机实测）

| 门禁 | 命令 | 结果 |
|---|---|---|
| Ruff check | `.venv/bin/ruff check backend tests` | ✅ All checks passed |
| Ruff format | `.venv/bin/ruff format --check backend tests` | ✅ 505 files already formatted |
| mypy strict | `.venv/bin/mypy backend` | ✅ Success: no issues found in 309 source files |
| 单元 + 契约 + RAG | `pytest tests/unit tests/contract tests/rag -q` | ✅ **1026 passed, 2 skipped** |
| 集成全套 | `pytest tests/integration tests/failure_recovery tests/conversation tests/community tests/study -q` | ✅ **431 passed, 3 skipped** |
| 迁移 | memory / conversation 链 `upgrade head` | ✅ 单 head、无冲突 |
| OpenAPI 快照 | `set(app.openapi()["paths"]) == set(snapshot["paths"])` | ✅ 一致；新增 3 条单数路径 + 8 个 schema，纯增量 |

> 说明：`tests/integration/test_backup.py::test_restore_replays_completed_deletion` 首次失败是**环境问题**（沙箱内 `uv` 缓存不可写，测试内部 `subprocess` 调 `uv run alembic`），把 `UV_CACHE_DIR` 指到可写目录后 9 项全绿，**不是代码缺陷**。

**测试无法覆盖的问题（本轮 4 个 Critical 全部属于此类）**

- 单测/集成测试**注入 Fake 网关**，从不经过 `conversation/worker/main.py` 签发的真实令牌 → 认证 scope 缺陷不可见；
- 集成测试调 `runner.run(batch)` **不传 `fencing`** → 真实 Worker 的 lease fencing 路径完全没被覆盖；
- 测试只断言对象存储命名空间（`objects/`）为空，**从不断言热缓存目录**（`threads/`）→ 删除合规缺口不可见。

---

## 2. 结论

架构与工程纪律是**高水准**的：`memory-rebuild-deviations.md`（1296 行）把绝大多数有意偏离登记得非常清楚，七个新 flag 全部默认关闭且关闭路径逐一验证为真 no-op，租户隔离在每条新 SQL / 每个新文件路径上都成立，migration 的 upgrade/downgrade、CHECK 约束与 Python `Literal` 逐项对齐（我逐个复核过 `memory_operations.status/operation_type`、`memory_commits.action`、`conversation_rollout_segments.status` 等，全部同步）。测试量与测试意图都很扎实。

但**新链路目前一条都不能真正启用**：Phase 5 与 Phase 6/7 各有一个致命的、只在真实装配下才暴露的缺陷，Phase 1–3 的删除语义有一个合规级缺口。这些都不是风格问题，而是「打开 flag 就坏」或「数据删不干净」级别的问题。

| 级别 | 数量 | 摘要 |
|---|---|---|
| **Critical** | 3 | scope 不匹配使 prime/tools 每轮必失败；真实 Worker fencing 使 nightly batch 永不写入；本地热缓存 JSONL 从不删除 |
| **Important** | 12 | 重试段注册失败、consolidation 用错 batch id、KG 映射被破坏、read hint 跳过未投递行、prime 无界、pin 审计死代码、turn_started 缺失、retention 死配置、kodo 装配点不一致、取消不释放成员、成员失败不隔离、`answer_stream_*` 触发 finalize 回滚 |
| **Minor / Nit** | 30+ | 见 §4 |

---

## 3. Critical

### C-1 启用 `memory_prime` / `memory_tools` 会让**每一轮对话直接失败**（scope 不匹配）

| 项 | 内容 |
|---|---|
| 位置 | `backend/conversation/worker/main.py:60-67`、`backend/memory/api/internal.py:307/320/333`、`backend/shared/auth_context.py:289` |
| 证据 | 签发端 `requested_scopes=[SCOPE_MEMORY_CONTEXT]`（`"memory:context"`）；端点端 `require(actors=_READ_AGENT_ACTORS, scope=SCOPE_MEMORY_READ)`（`"memory:read"`）；`has_scope` 是精确成员判定 |
| 实测 | 用真实 guard 复现：`scopes=['memory:context'] -> AuthError: 缺少 scope: memory:read (forbidden=True)`；补上 `memory:read` 后 `ALLOWED`。经 `app.exception_handler(AuthError)` 映射为 **403** |
| 后果 | `graph/nodes/memory.py:128-134`：prime 收到 4xx 走 `raise`（不可降级）→ 打开 `MEMORY_PRIME_ENABLED` 后**每轮 turn 在 recall_memory 阶段就失败**；`graph/nodes/memory_tool.py:254-262` `_is_turn_fatal` 把 403 判为 turn-fatal → 打开 `MEMORY_TOOLS_ENABLED` 后任何一次工具调用都会让 turn 失败 |
| 为什么测试没抓到 | 单测用 Fake 网关；没有任何测试用 `worker/main.py` 的真实 `issue_memory_context_token` 打真实端点 |
| 修复 | `issue_memory_context_token` 的 `requested_scopes` 加上 `SCOPE_MEMORY_READ`（它已在 `AGENT_ALLOWED_SCOPES` 白名单内，**一行改动**）；并补一条「真实签发 token → 打 tool 端点」的集成测试 |
| 登记情况 | **未登记**。ADD-048/ADD-055 只讨论了「认证靠 scope」的抽象语义，从未指出客户端令牌根本没有这个 scope |
| 当前影响 | 生产 `deploy/env.production.example` 未开这些 flag，因此**尚未引爆**，但这是启用前必须修掉的阻塞项 |

### C-2 真实 Worker 的 lease fencing 使 nightly batch **永远写不进任何事实**，并把用户证据永久搁浅

| 项 | 内容 |
|---|---|
| 位置 | `backend/memory/graph/batch.py:151`、`backend/memory/graph/summary.py:807-826`、`backend/memory/services/memory_service.py:398-415`、`backend/memory/persistence/operations.py:208-239`、`backend/memory/worker/worker.py:145/187-196` |
| 证据 | `begin_batch_member` 把 `state["operation"]` 换成**成员** operation（`return {"operation": member["operation"], ...}`），但 `state["fencing"]` 仍是**批次**的 `{"worker_id", "generation"}`（由 `runner.run()` 一次性写入，batch.py 从不改写）；提交节点把 `operation.operation_id`（成员）与 `fencing`（批次）一起传给 `commit_plans`；`mark_commit_started` 的 CAS 谓词是 `operation_id = :id AND locked_by = :worker AND lease_generation = :gen AND status = 'running'`，而成员行是 `status='pending_batch'`、`locked_by=NULL` → **恒 0 行 → 抛 `LeaseFencedError`** |
| 实测 | 探针（真实 PG、真实 Worker fencing 参数）：`LeaseFencedError … commit 标记 CAS 失败`、`COMMITS: 0`、成员仍为 `pending_batch` |
| 后果链 | Worker 对 `LeaseFencedError` 的分支是「记 warning、**不做任何状态写回**」（worker.py:187-196）→ 批次 operation 保持 `running` → lease 回收 → `retry_wait` → 再 claim → 同样失败，**没有 dead-letter 出口**；成员带 `batch_operation_id` 且状态 `pending_batch`，同时被 `list_pending_batch_members`（要求 `batch_operation_id IS NULL`）和 `_sweep_batch_runs`（要求无剩余证据）排除 → **证据永久搁浅，用户记忆永远不更新，LLM 预算按 max_attempts 白烧** |
| 为什么 CI 全绿 | `tests/integration/test_memory_batch_graph.py` 全部用 `await runner.run(batch)`（**不带 `fencing`**），因此走的是 `mark_commit_started` 的无 fencing 直调分支 |
| 修复 | 成员的提交必须用「真正持有 lease 的那个 operation」做 fencing（即批次 operation），成员 id 只用于重放键/可追溯；或者把每个成员做成独立 claim/lease 的 operation。无论哪种，都必须补一条**走真实 Worker（claim → run → complete）**的集成测试 |
| 登记情况 | **未登记** |

### C-3 本地模式下 thread 删除**从不删除热缓存 JSONL**，用户对话原文留在磁盘上

| 项 | 内容 |
|---|---|
| 位置 | `backend/conversation/rollout/object_store.py:97-101/190-194`、`backend/conversation/services/thread_deletion.py:77-106` |
| 证据 | 写入器把热段写到 `{root}/threads/YYYY/MM/DD/<thread>/<ordinal>-<seg>.jsonl`（`file_naming.segment_path`）；`LocalRolloutObjectStore` 的 bucket 根是 `self._root = Path(root) / "objects"`；删除只调 `object_store.delete(key=key)`（作用于 `objects/` 镜像）。全仓库 `backend/conversation` 里 `.unlink()` 只出现在 `object_store.py`，热缓存路径**没有任何删除者** |
| 实测 | 复现探针：删除 bucket key 后 `hot files AFTER deletion = ['threads/2026/09/10/<thread>/000000000000-<seg>.jsonl']`，`hot 文件里仍含用户原文: True`（3 行完整记录） |
| 后果 | ① §1.8 的「删除后数据物理消失」不成立——`delete_thread` 返回 `done`、manifest 已 tombstone，但**正文仍在磁盘**；② tombstone 之后没有任何工具能再找到它：`list_by_thread` 过滤 `status <> 'deleted'`，`retention-scan` 只扫 `status='sealed'`，`reconcile` 只列对象存储的 `rollouts/` 前缀 → 残留**永久不可发现、不可清理** |
| 为什么测试没抓到 | `tests/conversation/test_rollout_seal_read.py:459` 只断言 `object_store.list_prefix("rollouts/") == []`（镜像为空） |
| 修复 | 删除/retention/CLI 三处都要同时 unlink 热段路径（`local_path_for_object_key(root, key)` 或 `segment_dir`）；或让 local store 自己拥有并清理热路径 |
| 登记情况 | **未登记**（另外，同源的 Important I-6 也说明 flag 关闭时连镜像都不删） |

---

## 4. Important

### I-1 段重试后新段**永远登记不上**（唯一约束冲突被静默吞掉）

- 位置：`conversation_migrations/versions/0008_rollout_segment_turn_uq.py:28-31`、`rollout/sealer.py:102-136/146-162`、`persistence/rollout_manifests.py:44-53`
- 证据：`resolve_resume` 只找 `status='open'`；一旦 turn 的第一次尝试已把段**封存**（正常路径：图抛异常 → `finally: close_turn()` → `_seal_segment` 封存 → Worker 重新 claim 重试），重试会新建一个段，而 `uq_rollout_segment_turn_active`（`(turn_id) WHERE status <> 'deleted'`）**已封存的段并未排除**；`insert_open` 只写 `ON CONFLICT (segment_id) DO NOTHING`，于是抛 `UniqueViolation`
- 实测（真实 PG）：`duplicate key value violates unique constraint "uq_rollout_segment_turn_active"`；`[attempt 1] rows: [('0','sealed')]` → `[attempt 2] new ordinal_start=3`；`new segment registered? False`
- 后果：`register_open` 把异常吞成一行 warning（sealer.py:161）→ 新段没有任何 manifest 行 → `seal()` 的 `UPDATE … WHERE status='open'` 更新 0 行 → `reason=not_transitionable`，而对象**已经上传**（孤儿对象）；该 turn 之后的指针写入全部落空。同一缺陷在「跨节点恢复」（本地文件缺失 → 旧行标 deleted → 复用同一 `ordinal_start`）路径下表现为**另一个**约束 `uq_rollout_segment_thread_ordinal` 冲突（同样实测复现）
- 分析：`(thread_id, ordinal_start)` 的 UNIQUE **不排除 deleted 行**，因此「标废旧段 + 复用 ordinal_start」这个设计在约束层面就不成立；0008 只放宽了 `turn_id`，没放宽 `ordinal_start`
- 修复：①`(thread_id, ordinal_start)` 也改为部分唯一索引（`WHERE status <> 'deleted'`）；②`open_turn` 优先复用该 turn 已有的非删除段，而不是新分配；③`register_open` 失败必须升级为可观测降级，而不是一行 warning
- 登记情况：**未登记**（ADD-023 把「跨节点重建」写成了已实现的能力，但该路径在约束下不可用）

### I-2 consolidation 在 `finalize_batch_result` **之前**执行，拿到的是**最后一个成员**的 operation

- 位置：`backend/memory/graph/runner.py:159-160`（`enter_batch_consolidation → finalize_batch_result`）、`graph/consolidation.py:69-71`、`graph/batch.py:326-332`
- 证据：`batch_operation_id = operation.operation_id`，而此刻 `state["operation"]` 仍被 `begin_batch_member` 投影成成员（还原发生在**它之后**的 `finalize_batch_result`）
- 后果：所有末段产物的 `batch_operation_id` 都是**成员 id**——`memory_summary.meta.json` 的幂等凭证、`memory_dangling_links` 的 sightings/幂等键、KG 审计锚全部错位；更严重的是末段自己的治理提交（`consolidation.py:499-513`）会用「成员的 operation_id + 批次的 fencing」→ 一旦 I-1/C-2 修好，keywords/aliases/悬空建档会**全部落进 `failed`**
- 与登记不符：ADD-064 声称「循环结束由 `finalize_batch_result` 把 operation 还原成批次自身」——还原确实存在，但**在 consolidation 之后**，登记掩盖了顺序问题
- 修复：进入 consolidation 前先还原 `state["operation"] = state["batch_operation"]`，或让 consolidation 直接读 `state["batch_operation"]`

### I-3 `frontmatter_patch` 提交会**摧毁** mastery↔KG 映射，并让紧随其后的双路更新空转

- 位置：`backend/memory/services/memory_service.py:676-691`、`backend/memory/services/kg_dual_write.py:192-195`
- 证据：mastery 的提交路径**无条件**先 `deactivate_graph_links(...)`，再只对 `node_ids`（= `graph_node_ids_by_plan[i]`）重新 `upsert_graph_link`；consolidation 的 keywords/aliases 计划**不传** `graph_node_ids_by_plan` → `node_ids=[]` → 旧 link 全被置 inactive、新的一个都不建
- 实测（探针）：`LINKS AFTER CREATE: [{'node_id':'n9501','memory_version':1,'active':True}]` → `LINKS AFTER frontmatter_patch: [{… 'active': False}]`，文档 `active_version 1→2`；而 `_load_active_links` 要求 `active=true AND memory_version=:version` → 紧接着的 `_dual_write_kg` 对**刚被 patch 的主题**必然 `skipped/no_graph_mapping`
- 附带：双路更新传的是 **patch 之前**的 version/checksum（DEV-028 声称「已修为真实值」，但真实值取自 patch 前的 `documents` 快照）
- 修复：`frontmatter_patch` 不应做「先全灭再重建」；应按现有 link 的 node_id 在新版本上重新 upsert，且治理后再重读文档取 version/checksum

### I-4 `memory.read` 的截断续读提示**跳过从未投递的行**

- 位置：`backend/conversation/graph/nodes/memory_tool.py:450-477`
- 证据：`consumed = payload["line_offset"]`；`returned = len(str(payload["content"]).splitlines())`——`content` 是**完整切片**，而 `rendered` 已被 `_apply_char_cap(8000)` / `_apply_token_budget` 截断；于是 `hint["line_offset"] = consumed + returned` 指向「服务端取到的末行 +1」，而不是「模型真正看到的末行 +1」
- 实测（复核者跑通管线）：200 行 read → 字符上限只投递约 72 行 → hint 仍写 `line_offset=200` → **73–200 行模型永远看不到**，且没有任何错误
- 修复：hint 必须从**已投递的前缀**推算（统计 `rendered` 中的行数）
- 测试缺口：现有断言只检查 `"line_offset" in output`

### I-5 prime 的 `index_entries` 无条数上限，而登记声称的兜底**不存在**

- 位置：`backend/memory/services/memory_tools.py:428-443`、`backend/conversation/services/context_service.py:230-231`、`graph/nodes/memory.py:107-119`
- 证据：服务端把注册表**全部**行返回（`description` 字段上限 2000 字符），conversation 侧把整个 dict 直接塞进 snapshot / answer view；在 `backend/conversation/` 全量 grep **找不到任何 prime 的 token/条数裁剪**
- 与登记不符：ADD-047 明确写「条数上限交由 conversation 侧的 token 预算裁剪负责（§5.7）」——**那段代码不存在**
- 后果：主题多的用户首轮注入与 checkpoint 体积无界（ADD-047 自己承认「首轮注入会偏大，属已知代价」，但代价的兜底并不存在）
- 修复：服务端加条数上限或节点侧做 token 裁剪；同时订正 ADD-047

### I-6 flag 关闭时 thread 删除只 tombstone 不删对象 → 对象**永久不可达**

- 位置：`backend/conversation/services/thread_deletion.py:82-106`、`backend/conversation/worker/main.py:115/186`
- 证据：`job_worker.rollout_object_store` 只在 `if settings.conversation_rollout_enabled:` 分支内赋值；`object_store is None` 时只记 warning，**随后仍执行** `mark_thread_deleted(...)`（在 `else` 之外）
- 后果：enable → disable 之后再删 thread，对象（本地 `objects/` 或 Kodo）原样保留，而 `delete-thread-rollouts` 跳过 deleted 行、`retention-scan` 只扫 sealed、reconcile 不自动删对象 → 与 C-3 叠加成「删不掉的用户数据」
- 与登记不符：DEV-006 说顺序是「先删对象、再落 tombstone」，但这条路径**既不删对象也照样落 tombstone**
- 修复：对象存储的构造不该受 flag 约束（或缺少存储时**拒绝** tombstone 并让 Job 失败）

### I-7 `kodo` 对象存储只在 CLI 生效；worker 与 app 硬编码 Local

- 位置：`backend/conversation/worker/main.py:122`、`backend/app.py:812-818`、`backend/conversation/rollout/factory.py:23`
- 证据：worker 与 app 都直接 `LocalRolloutObjectStore(root=...)`；`build_rollout_object_store` 只在 `cli/rollout.py:81-84` 被 import
- 与登记不符：ADD-031 声称「worker、app、CLI 三个装配点共用 `build_rollout_object_store`」——对 app.py 与 worker 都不成立
- 后果：配了 `CONVERSATION_ROLLOUT_OBJECT_STORE=kodo` 后，段仍写/读本地磁盘，而 `verify-manifest` / `reconcile-orphans`（走 factory）去 bucket 里找 → 每条段都报 `sealed_object_missing`；冷读也永远命不中 Kodo
- 附带（**同类**）：`app.py:811/817/818/823` 用全局 `get_settings()` 而不是 `create_app(settings)` 的入参，显式注入 Settings 的场景（契约测试、内嵌 app）会拿到**另一个** Settings 对象

### I-8 取消批次 operation **不释放成员**；被取消的证据仍会被处理

- 位置：`backend/memory/persistence/operations.py:404-425`（`request_cancel` 直接 `SET status='cancelled'`）、`settle_batch_members` 只在 `complete_operation` 被调用、`list_batch_member_operations:658-673` 无状态过滤、`begin_batch_member:138-149` 只查 `memory_commits`
- 后果一：取消队列中的批次后，成员仍是 `pending_batch AND batch_operation_id IS NOT NULL` → 两个扫描都看不到 → 证据永久搁浅；`_sweep_batch_runs` 还会把该 run 收尾成 `succeeded`（掩盖问题）
- 后果二：`request_cancel` 是**明确允许**取消 `pending_batch` 的，被取消的成员仍会被批次 `SELECT *` 捞出来处理 → **用户取消了的证据照样写进长期记忆**，而行状态仍是 `cancelled`
- 与登记不符：ADD-066 登记了「cancelled → 释放成员」，但只有 `complete_operation` 实现了它，`request_cancel`（API/管理路径）没有

### I-9 没有成员级失败隔离：一条 poison 成员会让整批（最多 50 条）进死信

- 位置：`backend/memory/graph/summary.py:159-170`（只 `except LLMBudgetExceededError`）、`graph/batch.py:24-26`（docstring 声称「单成员失败不拖垮整批」）
- 证据：批内任何其它异常都会冒泡出图 → 批次 operation 失败 → 重试耗尽后 `dead_letter` → `settle_batch_members` 把**全部**成员置 `dead_letter`，**包括从未被尝试过的成员**
- 与文档不符：§5.8 要求「单成员失败记录 reason 并按既有 retry/dead-letter 语义处理」，实现只对 LLM 预算耗尽做了隔离
- 修复：在成员循环内捕获节点异常、记 `batch_failed` 并继续

### I-10 `answer_stream_*` 降级标记不在封闭 `DegradedFlag` 里 → finalize 整事务回滚，已流出的部分回答丢失

- 位置：`backend/conversation/contracts/api.py:48-59`、`graph/nodes/answer.py:229-235`、`persistence/event_writer.py:38`、`graph/nodes/finalize.py:144-161`
- 证据：`answer.py` 会 append `"answer_stream_interrupted"` / `"answer_stream_truncated"` / `"answer_stream_refused"`，但 `DegradedFlag` 是 `Literal`（只有 5 个旧值 + 本分支新增的 5 个 memory 值），`AnswerCompletedPayload` 是 `extra="forbid"`；`validate_event_payload` 在 **finalize 事务内**被调用且**没有 try/except** → 抛错回滚整个事务，turn 失败
- 严重度说明：**main 上就存在**（`git show main:.../api.py` 确认这 3 个值当时也不在 Literal 里），方向是 fail-closed 而非数据损坏；本分支既然正在扩展这个 Literal（DEV-014 专门讨论过「新增 flag 必须同步扩展」），顺手补齐成本极低；另外工具循环把 answer 节点最多跑 6 次，触发概率上升
- 修复：把 3 个 `answer_stream_*` 加进 `DegradedFlag`，并用 `validate_event_payload` 补一条实测

### I-11 索引投影的三处既有缺陷（v1→v2 迁移不回填 / `related_topic_keys` 滞后 / `name` 不投影）

- `alembic/versions/0008_memory_index_v2_columns.py:11-12` 的 docstring 说「回填由 Phase 4 的 `migrate_markdown_schema_v2` 逐用户任务完成」，但 `graph/maintenance.py:386-465` 的处理器只写 version + current，**从不调 `_upsert_index_entry` / `mark_index_dirty`** → 所有被迁移文档的 `aliases/keywords/related` 在下次记忆提交前一直为空，而集成测试从不检查投影
- `services/memory_service.py:378` 的 `related_topic_keys` 取 `mbase.links`，而 `mbase` 是**上一版**解析结果，`apply_mastery_patch` / `apply_frontmatter_patch` 都不重算 `.links` → 首次写入含 `[[抛物线]]` 的文档投影为空，之后**恰好滞后一个提交**；§5.6 验收里的「link 路由」不成立（未登记）
- `services/memory_service.py:373/1331` 的注册表 `title` 取 `topic_title` 而非 v2 frontmatter `name` → 改 `name` 的 `frontmatter_patch` 永远到不了注册表与 search（DEV-023 只登记了 `description` 的同类差异）

### I-12 前端镜像漏了本分支新增的 `frontmatter_patch`

- 位置：`frontend/src/api/memory.ts:172`
- 证据：TS 联合类型仍是 `"create" | "merge" | "replace" | "append_evidence" | "forget" | "restore"`，而 `backend/memory/contracts/operations.py:43-52` 已新增 `"frontmatter_patch"`
- 后果：纯类型层漂移（当前没有前端读 `.action`），但这是本分支唯一改动的前端文件，属明显遗漏

---

## 5. Minor / Nit（按主题归类，不再逐条展开证据）

**死配置 / 未接线**

- `MEMORY_SUMMARY_LLM_CONCURRENCY` 全仓库无人读取（`EvidenceBatchLimits.llm_concurrency` 无生产构造方），批内成员实际是**串行** while 循环 → §2.6 D4 的「有界认领三件套」缺一件
- `CONVERSATION_ROLLOUT_RETENTION_DAYS` 全仓库无人读取，retention 必须每次手工传 `--older-than-days` → 自动化 §1.8 retention 实际未接线
- `rollout_queue_depth` / `rollout_flush_latency_seconds` / `rollout_sealed_total` 三个指标**声明但零发射点**（ADD-020 声称已实现）；封存失败只有一行日志
- `runtime.rollout_reader` 在任何 composition root 都不赋值（只有 `app.py:815` 为 source-read 服务构造了一个局部 reader）→ `graph/nodes/memory.py:193` 恒得 `None`，DEV-015 描述的「pin hash 变化补记」在生产**不可达**（单测靠注入 FakeRolloutReader 才过）
- Kodo 适配器的 `region` 存而不用；`read_timeout_seconds` 只折进全局 `connection_timeout=max(...)`，`fetch` 硬编码 `timeout=30`
- `MEMORY_SCHEMA_V2_READ_ENABLED` 无人读取（ADD-036 已登记「解析器始终双读」，但 flag 名与实际语义相反，属误导性死配置）
- 启动时的 `validate_schema_prompt_binding()` 是同模块内两个常量的自洽检查（永真），不能发现真实部署错配

**指标 / 可观测性**

- `memory_operations_total{status="queued"}` 在行实际以 `pending_batch` 插入时仍打 `queued` 标签（`api/dependencies.py:210/262`）→ 开批量后该指标失真
- 若干降级标记只进 warning、不进结构化字段（`memory_prime` 的 `summary_truncated`、工具循环触顶时最后一次工具请求被丢弃且**无任何信号**）
- `memory_operations_total` 之外，C-2 的「无限重试」没有任何 metric 会暴露

**调度 / 批处理**

- `list_open_runs` 先 `ORDER BY created_at ASC LIMIT :limit` **再**按 `endswith(f":{date}")` 过滤 → 超过 limit 的历史残留会**永久挤占**当天 sweep 的名额
- `_batch_cursor_tuple` 的 `BatchContractError` 在**单个共享事务**里抛出，会回滚当晚**所有用户**的入批（`run_task` 吞掉异常 → 下一次要等到明天 0 点）
- `memory_maintenance_runs` 没有 `(maintenance_type, status)` 索引，`list_open_runs` 在 30 秒续跑 tick 上全表扫
- 批次 operation 的幂等键把 cursor 摘要截到 64 bit（`sha256(...)[:16]`），碰撞会让用户当天剩余证据白等一天
- `_sweep_batch_runs` 在 `get_operation` 返回 `None` 时（run 有 operation_id 但行不存在）不会 `continue`，会继续走「无剩余证据就关 run」的分支（低概率加固项）

**契约 / 解析**

- `line_offset` 是 **0-indexed**，而 §2.4 D3② 明确要求对齐 codex 的 1-indexed（未登记；内部自洽，无数据损坏）
- `markdown_schema._parse_index_blocks`：未知行**静默跳过**（v2 文档里混入 v1 单行条目会解析成 0 条）、`version` 非数字回退 `0`、缺 `updated_at` 回退 `datetime.now(UTC)`（**解析不确定**）——与 v1 路径抛 `MarkdownParseError`、与模块自身「解析失败驱动一致性告警」的承诺相矛盾
- `build_search_sql` 在 `queries` 为空时会拼出非法 SQL（`AND (\n \n )`），只靠唯一调用方的提前返回保护
- `memory_tool.py:36` 重复声明 `MEMORY_TOOL_CALL_BUDGET = 6` 而非 import `contracts/rollout.py` 的常量（ADD-056 声称「直接复用」，实际会漂移且漂移后**静默丢 rollout 记录**）
- `_normalize_arguments` 只 strip 空白，没有 ADD-056 声称的「去重、边界裁剪」：9 条 query 或 >200 字符不裁剪，直接烧预算换回 422；`["a","a"]` 与 `["a"]` 是两个缓存键
- `build_mutation_plan_v2.md` 让模型「把 name/description/aliases 写进正文与 reasoning_summary，**不得自行输出 frontmatter 字段**」，而应用层只读结构化 `frontmatter_patch` → 服从指令的模型会产出「v1 文档 + 空 v2 frontmatter」（行为后果未证实，但提示词与落地通道自相矛盾）
- `CommitMutationPlan` 的跨字段校验不要求 `frontmatter_patch` 带 `expected_version`（ADD-088 已登记；运行期仍会抛冲突，不会静默覆盖）
- `related_topic_keys` 在 index 块里用 `|` 分隔，值本身含 `|` 时不可往返（ADD-089 已登记）

**删除 / 合规**

- 集成测试 `test_account_purge.py` 从未 seed / 断言 `pending_batch`，尽管 `drain_user_operations` 与 `request_cancel` 都为本分支改了这个状态
- `conversation.conversation_rollout_segments` 没有账号级 purge 路径（本仓库 conversation 域没有账号删除实现，只有 thread 级软删），`tests/conversation/conftest.py` 有 truncate、但没有任何断言覆盖热文件或账号级删除

**文档 / 登记一致性**

- **ADD-064**：声称 finalize 还原 operation —— 还原发生在 consolidation **之后**（见 I-2）
- **ADD-066**：登记「cancelled → 释放成员」，只有 `complete_operation` 实现（见 I-8）
- **ADD-047**：声称 conversation 侧有 token 裁剪兜底 —— 不存在（见 I-5）
- **ADD-031**：声称 worker/app/CLI 三处共用 factory —— app.py 与 worker 都不成立（见 I-7）
- **ADD-020**：声称 rollout 指标已实现 —— 3 个指标零发射点
- **ADD-056**：声称复用契约常量 + 缓存键「去重、边界裁剪」 —— 均不成立
- **DEV-027**：只登记了 mastery 的 `search_text` 差异，learner 侧同样存在（提交侧含 `*base.aliases`，restore 侧不含）
- **DEV-011**：称「keywords 档是死代码」—— Phase 7 已补上生产者，该登记已过时
- **OPEN-005**：称「reconcile/CLI 没有仓库内集成测试」—— 同分支 Phase 3 已补 `test_rollout_cli_integration.py` / `test_rollout_reconcile_integration.py`，未关闭（对照 OPEN-003 已关闭）
- **未登记**：C-1、C-2、C-3、I-1、I-2、I-3、I-6、I-9、I-11、0-indexed `line_offset`、`MEMORY_SUMMARY_LLM_CONCURRENCY` 空转、`CONVERSATION_ROLLOUT_RETENTION_DAYS` 空转、`turn_started` 缺失、`rollout_reader` 未接线
- `tests/unit/test_markdown_schema_migration.py:8-9` 的 docstring 仍说 DB 集成测试「尚未补（见 PHASE4-HANDOFF.md 第 8 项）」，而 `tests/integration/test_memory_schema_migration.py` 已存在、OPEN-007 已关闭
- `cli/rollout.py:11-15` 的 docstring 说「对象存储固定用 LocalRolloutObjectStore」「配置成 kodo 时显式失败」，与代码（走 factory、可构造 Kodo，只有凭据缺失才失败）不符
- `.env.example` 缺 `MEMORY_CONSOLIDATION_INPUT_MAX_CHARS=48000`（ADD-076 修过同类的 `MEMORY_BATCH_ENABLED` 遗漏），也缺 `MEMORY_SCHEDULER_TIMEZONE`（新注释却把它当作既有变量引用）
- `alembic/versions/0011_...` 依赖自动生成的约束名 `memory_commits_action_check`（真实库确认存在），但 `DROP CONSTRAINT IF EXISTS` 在异构基线上会静默不删旧约束

**其它**

- `0008_rollout_segment_turn_uq.downgrade` 的守卫只检查**非 deleted 重复行**，随后却重建覆盖全部行的 `UNIQUE (turn_id)` → 在它本来要允许的「1 deleted + 1 活跃」状态下回滚必然 `UniqueViolation`（复核者已在临时库复现），而不是给出设计的 `RAISE EXCEPTION` 提示
- `list_user_links` 无 LIMIT 且直接喂给 index rebuild（链接多的用户全量加载）
- recorder resume 时**不截断崩溃留下的半行**（`open("ab")` + `st_size`），下一次 append 会与之粘成一条损坏记录（`iter_records` 会因此中止整段）；只有写失败重试路径做了截断
- `recorder._segment_date` 用 `parts.index("threads")` 反解路径，根目录含 `threads` 组件时静默回退 `datetime.now(UTC)`（而 `handle.thread_created_at` 就在手边）
- `reconcile._POINTER_SQL` 选了 `s.status AS segment_status` 却从不读它 → 「指向已软删段的指针」这一盲区不上报
- `_enqueue_line` 失败时已分配的两个 ordinal 不回滚，留下序号空洞
- `conversation_rollout_segments` 的 `BEFORE DELETE` 触发器在应用层全部走软删的前提下不可达（也无测试）；`rollout.private_url`（Kodo 签名 URL）无任何调用方，`kodo_cdn_domain` 实际不影响运行时
- `contracts/batch.py` 的 `EvidenceBatchPlan` / `validate_member_transition` / `BATCH_MEMBER_TRANSITIONS` / `is_member` / `EXEMPT_TRIGGERS` 在生产代码里**没有消费者**；`_check_limits_and_isolation` 的名字暗示会校验成员归属，实际没有
- ADD-054 声称「关闭 tools 时 `answer.completed` 形状不变」，但 `build_answer_completed_payload` **无条件**输出 `memory_citations: []`
- `frontend/src/api/memory.ts` 新增 `pending_batch` 后，若该状态真的被前端轮询会一直轮询到 2 分钟超时（当前无触达路径）

---

## 6. 已核实为「干净」的部分（覆盖度证明）

**安全 / 隔离**

- 记忆工具三个端点的 `user_id` 只取自 `auth.user_id`，请求体 `extra="forbid"`（夹带 `user_id` → 422 `REQUEST_EXTRA_FIELD`）；复数路径别名共用同一 handler 与依赖，无越权面
- `fetch_index_projection` / `build_search_sql` / `fetch_search_rows` / `read_active_content` 全部带 `user_id` 过滤；`read` 对「不存在 / 已删 / quarantine」统一 `MEMORY_NOT_FOUND`，不泄露跨用户存在性
- `LocalMarkdownStore._abs` 的 resolve + parents 校验、`_validate_key_part`、local/Kodo 对象 key 的绝对路径与 `..` 拒绝；Kodo 凭据不入日志/异常/指标
- LIKE 元字符经 `escape_like` + `ESCAPE '\'` 转义且全部参数绑定（集成测试覆盖 `100%` / `%` / `0%_`）
- 工具结果与 prime 内容以 **JSON 值**进入提示词（`LONG_TERM_MEMORY` / `function_call_output`），不是裸插值
- `.env.example` 不含任何密钥（`KODO_ACCESS_KEY` / `SECRET_KEY` 留空）；`kodo` 模式缺配置在 Settings 构造期即失败，没有静默降级（也没有被早退绕过）

**契约 / 数据完整性**

- 七个新 flag 全部默认关闭，且关闭路径逐一验证为真 no-op（`record_rollout` 无 recorder 直接 no-op、`_evidence_batch_gate` 返回 `("queued", None)`、claim 查询仍只认 `('queued','retry_wait')`、`_route_after_answer` 恒 `validate`、consolidation 返回 `disabled`、KG 双写返回 `skipped`）
- DB CHECK 与 Python `Literal` 逐项一致（`memory_operations.status` 8 值、`operation_type` 17 值、`memory_commits.action` 8 值含新增 `frontmatter_patch`、segment status 3 值、dangling link status 3 值、rollout record type 12 值）；两条链各只有 1 个 head，revision id 长度未越 `varchar(32)`
- 迁移 upgrade/downgrade 在真实库上跑通（memory 链 `head → base → head`；conversation `0007/0008` 双向），破坏性收窄都带 `RAISE` 守卫；`memory_dangling_links` 已进 purge 清单 + conftest + 断言三处，自引用 FK 不破坏 purge 的单语句删除
- `settle_batch_members` 与批次终态写回**同事务**；`insert_operation` 的 `status` / `next_run_at` 默认值与 0001 DDL 一致；`_tighten_pending_batch_gate` 只许把门控提前
- 幂等：`assign_batch_members` 带 `batch_operation_id IS NULL` 守卫；批次幂等键长度受控（DEV-018）且可由 cursor 确定性重建；悬空链接 upsert 在**单条语句内**完成「判定 + 累加」，并发重跑不会多算；KG 幂等键含 `node_id`，uuid5 追溯锚是 `status='cancelled'`（worker 永不认领）
- markdown v1/v2 双读无条件生效，v1 round-trip 保持 v1；缺失 `schema_version` 默认 v1；v2 要求单行 description；unicode topic key / aliases / `[[link]]` 往返正常
- 工具循环的两个上限（调用 6 / 轮数 6）都真实生效，缓存命中不消耗调用预算且不伪造 `call_index`；截断后的结果始终能重新解析成合法 JSON（已跑通管线验证）
- 错误分类矩阵（404 / 其它 4xx / 5xx / 超时 / 401-403）与测试一一对应
- 读路径顺序与 tombstone 抑制（`status='open'|'sealed'` 且非 deleted）、字节区间 **与** ordinal 双重校验、sha256 复核、「宁可拒绝也不返回错正文」、本地优先再对象的顺序、`thread_deletion` 的「先删对象再 tombstone」在 flag 打开时确实成立

---

## 7. 建议的合并前必改清单（按投入产出排序）

| # | 项 | 改动量 |
|---|---|---|
| 1 | **C-1** `issue_memory_context_token` 加 `SCOPE_MEMORY_READ` + 一条真实令牌的集成测试 | 1 行 + 1 测试 |
| 2 | **C-3 / I-6** 删除、retention、CLI 三处同时 unlink 热缓存；对象存储构造脱离 flag；补「热文件也不存在」断言 | 小 |
| 3 | **C-2** 成员提交改用批次 operation 做 fencing（或让成员各自持有 lease）；补真实 Worker 端到端测试 | 中 |
| 4 | **I-1** `(thread_id, ordinal_start)` 改部分唯一索引 + `open_turn` 复用已有非删除段 + `register_open` 失败升级为降级 | 中（含迁移） |
| 5 | **I-2** consolidation 前还原 `state["operation"]` | 小 |
| 6 | **I-3** `frontmatter_patch` 不再「先全灭再重建」；治理后重读 version/checksum | 小 |
| 7 | **I-4** read hint 按**已投递**前缀推算 | 小 |
| 8 | **I-10** `DegradedFlag` 补 3 个 `answer_stream_*`（顺手做完，与 DEV-014 同一类问题） | 小 |
| 9 | **I-5 / I-7 / I-8 / I-9** prime 加界、kodo 装配点统一、取消释放成员、成员级失败隔离 | 中 |
| 10 | 把 §5 的「未登记」项补进 `memory-rebuild-deviations.md`，并订正 ADD-020/031/047/056/064/066、DEV-011/015/027、OPEN-005 这些与代码不符的条目 | 小（但必须做） |

> 由于所有新链路在生产配置里都是默认关闭的，本分支合并**不会**破坏当前线上行为；但上面 1–4 项修完之前，任何一个 flag 都不能批准启用。
