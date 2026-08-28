# Memory 框架重构设计（memory-rebuild）

> 本文档是记忆框架重构的系列设计文档，每章覆盖一个记忆域的改造。
> 参考项目：`codex-rs/`（只读参考，禁止修改）。设计讨论定稿后再实施，遵守
> "实现不等批准，启用必须等批准"：所有新链路默认 feature flag 关闭。
>
> 部署前提：**最终部署在云服务器，非本地单机；支持对象存储**。这一前提直接
> 影响第一章的文件写入设计（见 §1.7）。
>
> 引用约定：凡借鉴 codex-rs / OPUS-5 的设计点，正文均注明**原因 + 模仿的代码位置**
> （文件 + 行号）；§1.2 / §2.1 / §3.1 三张表是集中索引，正文散点引用在表外补充
> 具体位置。

---

## 第一章 Conversation 短期记忆 Rollout 化（conversation-rollout-design）

### 1.0 设计思路（一句话）

把短期记忆从"只有 LangGraph checkpoint 一种不透明载体"改造为 codex-rs 的三层模型：
**内存（图执行工作态）→ JSONL rollout 文件（唯一事实源、人类可读、可独立重放）→
PostgreSQL（索引 + 分布式协调）**。

### 1.1 现状盘点（代码核实，非猜测）

短期记忆在规格中的定义（[memorymangergraph.md](memorymangergraph.md) §3.1）：
会话线程状态，由对话 Agent 的 LangGraph Checkpointer 保存，不进入 Markdown。

当前实现的真实状态：

| 载体 | 内容 | 代码位置 |
|---|---|---|
| LangGraph checkpoint（PostgreSQL saver） | 整个 `ConversationGraphState`：`snapshot`（**内联最近 20 条消息全文**）、`rewrite_plan`、`evidence_set`、`answer_buffer` 等 | [backend/conversation/worker/main.py](backend/conversation/worker/main.py)（`AsyncPostgresSaver`，独立 schema `conversation_checkpoints`）；graph thread = `conv-turn:{turn_id}`（**每轮一个 thread**），见 [backend/conversation/graph/runner.py](backend/conversation/graph/runner.py) |
| `conversation_messages` 表 | **消息全文 canonical**：content / content_hash / `eligible_for_context` / `eligible_for_memory` / 软删除 | [backend/conversation/persistence/messages.py](backend/conversation/persistence/messages.py) |
| `conversation_turn_events` 表 | 每轮事件流水：`turn.started` / `answer.delta`（文本增量）/ `answer.completed`（**完整回答 + citations**）等，sequence 由锁 Turn 行原子 +1 分配 | [backend/conversation/persistence/events.py](backend/conversation/persistence/events.py)、[backend/conversation/contracts/api.py](backend/conversation/contracts/api.py)（179–218 行 payload 定义） |
| `conversation_turns` 表 | 状态机 / lease / fencing / `last_event_sequence` 序号分配器 | [backend/conversation/persistence/turns.py](backend/conversation/persistence/turns.py) |
| SSE 断线重放 | `Last-Event-ID` → 按 `(turn_id, sequence)` 从 turn_events 补发；早于最早保留事件 → 410 `EVENT_REPLAY_EXPIRED` | [backend/conversation/api/events.py](backend/conversation/api/events.py) |
| 跨轮上下文 | 每轮由 `ContextService` 从 messages 表重建 `TurnContextSnapshot`（最近 20 条 / 6000 tokens）再塞入 checkpoint | [backend/conversation/services/context_service.py](backend/conversation/services/context_service.py) |
| Retention | 30 天 turn event 清理 + 终态 checkpoint 清理（maintenance loop，advisory lock 防多副本并发） | [backend/conversation/worker/graph_worker.py](backend/conversation/worker/graph_worker.py) `run_maintenance_loop` |

**现状已有的冗余（有意分层，非失误）**：

1. 助手回答全文两份：`conversation_messages`（长期 canonical）+ `turn_events.answer.completed`（30 天短命，服务 SSE 重放）。
2. checkpoint 的 `snapshot.recent_messages` 内联消息全文——LangGraph 恢复要求 checkpoint
   自包含（方案 §9.3：恢复时禁止重读 DB），代价是短命副本。

**真正的差距**：短期记忆没有任何"按会话组织、人类可读、可独立重放"的载体。
checkpoint 是不透明 blob、按 turn 切片、终态即被 retention 清除；排障、导出、
崩溃后审计都无从下手。这正是 codex-rs rollout 机制解决的问题。

### 1.2 codex-rs 参考机制与抄代码位置

| 机制 | codex-rs 代码位置 | 要点 | 我们抄什么 |
|---|---|---|---|
| 双写：先内存后落盘 | `codex-rs/core/src/session/mod.rs` `record_prepared_conversation_items`（3091–3120 行） | item 先写 `state.history`（ContextManager），再 `persist_rollout_items` 异步落盘 | 图节点产出时先更新 Graph State，再调 rollout recorder |
| RolloutRecorder | `codex-rs/rollout/src/recorder.rs` | 后台 tokio writer task + 有界 mpsc(256) 通道；**延迟建文件**（首个 persist 才物化）；`persist/flush` 带 oneshot ack；写失败重开文件重试 | Python 版：`asyncio.Queue(maxsize=256)` + 后台 task 持有文件句柄 |
| **时间戳 ① 文件名/会话创建时间** | `codex-rs/rollout/src/rollout_file_name.rs` | `rollout-<YYYY-MM-DDTHH-MM-SS>-<thread_id>[_<rollout_id>].jsonl`，秒级、可排序、可解析；revert 时追加 `_<rollout_id>` 保持 thread_id 稳定 | 文件名取 **thread 创建时间**；预留回滚命名规则 |
| **时间戳 ② 每行落盘时间** | `codex-rs/rollout/src/recorder.rs` `JsonlWriter::write_rollout_item`（1948–1966 行） | 每行 `{timestamp, ordinal, item}`；`timestamp = OffsetDateTime::now_utc()` 毫秒级 `...SS.sssZ`；`ordinal` 单调递增；**每行 write+flush** | 每行 `recorded_at`（UTC 毫秒）+ 文件级 `ordinal`；逐行 flush |
| 目录分片 | `codex-rs/rollout/src/recorder.rs`（1613–1624 行附近） | `sessions/YYYY/MM/DD/rollout-...jsonl` | `{rollout_root}/threads/YYYY/MM/DD/` |
| 首行 SessionMeta | `codex-rs/rollout/src/recorder.rs`（859–887 行） | 首行元数据：session_id / timestamp（毫秒，与文件名同源）/ cwd / source / cli_version 等 | 首行 `thread_meta`：thread_id / user_id / created_at / graph 版本 / schema_version |
| 持久化白名单 | `codex-rs/rollout/src/policy.rs` `is_persisted_rollout_item` | 消息/工具调用/Compaction/TurnContext 落盘；delta、审批请求等瞬态事件不落 | `backend/conversation/rollout/policy.py`：模型输入语义相关的落，瞬态传输态不落 |
| 重放重建 | `codex-rs/core/src/session/rollout_reconstruction.rs` | 反向扫描找最近 replacement-history 基线（compaction 快照），正向重放尾部重建内存 history | checkpoint 失效时从 jsonl 重建 `TurnContextSnapshot`（见 §1.6） |
| 索引分层 | `codex-rs/rollout/src/recorder.rs` `list_threads_with_db_fallback` | jsonl 是 source of truth；SQLite state_db 只做列表/搜索索引，read-repair 对齐 | PostgreSQL 扮演其 SQLite 角色（索引），**不抄 state_db 本身** |
| 归档 | `codex-rs/rollout/src/lib.rs` `ARCHIVED_SESSIONS_SUBDIR` | `archived_sessions/` 归档目录 | `archived_threads/`（见 §1.8） |

**明确不抄的部分**：SQLite state_db 本体（我们已有 PG 索引表）、Legacy/Paginated 双历史
模式、压缩 worker（`compression.rs`，量级不到）、multi-agent / world-state 条目。

### 1.3 目标架构

```text
ConversationGraph 执行中
  ├─ 内存态：ConversationGraphState（LangGraph 运行时，不变）
  ├─ 恢复缓存：AsyncPostgresSaver → conversation_checkpoints
  │    （保留但瘦身：终态即清，只是"加速恢复的缓存"，不再是唯一载体）
  └─ 事实源：TurnRolloutRecorder（graph 节点关键产出时调用）
        └─ asyncio.Queue(256) → 后台 writer task（逐行 write+flush）
              └─ {rollout_root}/threads/YYYY/MM/DD/
                   rollout-<thread_created_at>-<thread_id>.jsonl
                          ↓（turn 终态后封存，见 §1.7）
                   对象存储（云部署的持久层）
```

**职责划分**：jsonl 是短期记忆内容的**唯一事实源**；PG 只存索引与协调；
checkpoint 退化为可再生的恢复缓存。排障用 `jq` 直接读 jsonl；导出/审计不依赖 DB。

### 1.4 PostgreSQL 表分工（"PG 只存索引"的落地）

| 表 | 改造后定位 |
|---|---|
| `conversation_messages` | **索引化**：message_id / thread_id / sequence / content_hash / 资格标记 / 软删除位 + `(rollout_file, byte_offset)` 指针。**正文移出，只在 jsonl** |
| `conversation_turns` | **保留，定性为"协调"而非索引**：lease / claim / fencing / expected_thread_version 是分布式协调原语，jsonl 给不了（codex 没有它是因为本地单进程不需要） |
| `conversation_turn_events` | **过渡组件，第二阶段可废弃**（见 §1.6 SSE 分析） |
| `conversation_checkpoints` | 恢复缓存；终态清理逻辑不变 |
| outbox / jobs / knowledge_summaries | 不动——领域业务表，不属于短期记忆 |

### 1.5 JSONL 文件设计

**粒度：按 conversation thread**（不按 turn）。规格 §3.1 定义短期记忆属于会话线程；
graph thread 的 `conv-turn:{turn_id}` 切片是实现细节。同一 thread 的所有 turn 追加进
同一**逻辑文件**（物理载体 = 每 turn 一个不可变 segment，完整 thread 文件是 manifest
拼装出的逻辑视图，无任何进程持有跨 turn 的文件句柄——见 §1.7"多副本写入模型"），
`turn_started` / `turn_completed` 行作为段边界（对应 codex 的
TurnStarted/TurnComplete 段机制：`codex-rs/core/src/session/rollout_reconstruction.rs`
中 `ActiveReplaySegment` 以 TurnStarted/TurnComplete 事件划分重放段——193 行
TurnComplete、252 行 TurnStarted 触发段结算；抄它的原因：段边界是断点重放时
划分 turn、定位恢复基线的唯一可靠依据，我们把同样语义落到 jsonl 行级）。

**命名与分片**（照抄 `rollout_file_name.rs` 语义）：

```text
threads/YYYY/MM/DD/rollout-<thread创建时间,秒级>-<thread_id>.jsonl
# 预留：回滚到第 N 轮时 rollout-<ts>-<thread_id>_<rollback_id>.jsonl，thread_id 保持稳定
```

**行格式**：

```json
{"recorded_at": "2026-08-28T05:41:07.123Z", "ordinal": 42, "type": "user_message", "turn_id": "...", "payload": {...}}
```

- **时间戳 ①**：thread 创建时间 → 文件名（秒级，可排序）+ 首行 `thread_meta.created_at`（毫秒）
- **时间戳 ②**：每行 `recorded_at` = writer 实际落盘时间，UTC 毫秒（对应 codex `OffsetDateTime::now_utc()`）
- `ordinal`：文件级单调递增（与 turn 内 event sequence 并存，两者作用域不同）
- `turn_id`：每行冗余携带，单文件可按轮过滤

**记录内容白名单**（对应 codex `policy.rs`，落到 `backend/conversation/rollout/policy.py`）：

| 落盘（全文） | 落盘（仅引用） | 不落盘 |
|---|---|---|
| `thread_meta`（首行） | `evidence_set`：引用 ID + 排序（chunk 正文在 rag 库，体积大且不属于短期记忆） | SSE delta / answer_buffer 流式中间态 |
| `turn_started` / `turn_completed`（含 status、degraded_flags） | `embedded_queries`：只记模型标识 + 维度 | 取消令牌、lease 等瞬态控制 |
| `user_message` / `assistant_message`（正文全文） | | gateway 原始 HTTP 细节 |
| `turn_context_snapshot`（摘要级：snapshot_hash + 消息 ID 序列 + token 数 + memory_status） | | |
| `rewrite_plan`（每 revision） | | |

原则：**小文本放全文、大对象放引用**。消息文本 KB 级，全文廉价；evidence chunk /
向量是 MB 级潜在体积，只存引用。

**写入与故障语义**（照抄 recorder.rs）：

- 注入点：`ConversationRuntimeContext` 增加 `rollout_recorder`（可空）；
  `snapshot` / `rewrite` / `evidence` / `answer` / `finalize` 节点各追加一条；
  `finalize` 后 `flush()` 等 ack
- **逐行 write + flush**（对齐 codex 1972 行 `file.flush()`）：最小化崩溃窗口
- 延迟建文件：首个 flush 才物化，空跑 turn 不产生空文件
- 写 IO 失败 → 缓冲保留、重开文件重试一次；仍失败记 `rollout_write_failed` 指标并
  **降级不拖垮 turn**（此阶段 checkpoint 仍是恢复权威）
- 并发：同一 thread 的 turn 已被 DB lease 串行化；writer 按 thread_id 分桶持有句柄
- feature flag `conversation_rollout_enabled` 默认关闭

### 1.6 恢复路径与 SSE 断线重放

**图恢复**（沿用附录 A.3 决策树 + 新增第 ④ 分支，[backend/conversation/graph/runner.py](backend/conversation/graph/runner.py)）：

1. 有 checkpoint → resume（不变）
2. 无 checkpoint → 从 START 新跑（不变）
3. checkpoint 反序列化失败 → 记 `checkpoint_recovery_failed` 指标重跑（不变）
4. **新增**：终态 checkpoint 已被 retention 清除、需查看/导出/重建该会话短期记忆
   → 从 jsonl 重放（对应 `rollout_reconstruction.rs`）：反向找最近
   `turn_context_snapshot` 行，按消息 ID 序列回查 messages 索引，正向重放尾部。
   注意语义差异：codex 重放重建**模型输入**；我们的快照正文在 jsonl/索引，重建很轻。

**SSE 断线重放：两阶段决策**。

现状依赖（[api/events.py](backend/conversation/api/events.py)、[turns.py](backend/conversation/persistence/turns.py)）：
① sequence 由锁 Turn 行原子 +1 分配，**四个写入方**（API / Graph Worker / Publisher /
MEMORYACK）跨进程追加同一 turn；② 事件随事务 commit 落盘，无丢失窗口；
③ 410 过期判定查表一行。

jsonl 直接替代要补四个洞：

| 洞 | DB 免费给的 | jsonl 方案要做的 |
|---|---|---|
| 崩溃窗口 | 事件随事务 commit | 逐行 flush + 接受"最终一致 + 读时修复"；worker 在 finalize 提交与 jsonl flush 之间死掉会丢尾 |
| 跨进程排序 | 行锁原子 +1 | flock + ordinal 单点分配；`turn.accepted` 由 API 进程写的现状要改为 worker claim 后补写 |
| 410 判定 | 查表 | PG 索引表维护"每 turn 最早保留 ordinal" |
| 部署假设 | DB 天然共享 | 多副本 append 同一文件不成立，须由对象存储分段方案兜底（§1.7） |

**决策**：

- **第一阶段**：保留 `conversation_turn_events` 作为瘦身 SSE journal（只存 SSE 需要的
  事件类型，30 天清理）。jsonl 先在短期记忆这条**用户不可见**的路径上验证可靠性
- **第二阶段**：以崩溃注入测试验收（kill -9 worker 后 jsonl 与 turn 终态一致率），
  达标后 SSE 重放源切到 jsonl，events 表降级为"最早保留序号"一行索引，最终废弃

不一步到位的原因：SSE 重放是用户可见的实时正确性（重连少一条 answer.completed
= 回答消失）；codex 敢全押 jsonl 是因为本地 CLI 崩溃最大代价是自己会话丢尾，
我们丢的是用户看到的回答。

### 1.7 云部署与对象存储

前提：生产在云服务器，jsonl 持久层走对象存储。对象存储**不支持 append**，
因此写入路径与持久路径分离：

```text
写入路径（热）：节点本地盘（或共享块存储）追加，逐行 flush —— 低延迟、可 fsync
封存路径（冷）：turn 终态 / 段大小达阈值 / 节点退出前
  → 把该 turn 的段（segment）整体上传为对象：
    s3://.../threads/<thread_id>/segments/<ordinal_start>-<ordinal_end>.jsonl
  → PG 索引表记录 segment 清单（manifest）：thread_id / ordinal 范围 / object key / 行数 / crc
```

- **分段而非整文件**：单个 turn 一个段（天然边界），避免整文件重写；manifest 存 PG
  索引表——这正是"PG 只存索引"在存储层的体现
- **多副本**：任意时刻同一 thread 只有一个持 lease 的 worker 在写（现状已保证），
  段封存后不可变，天然免疫多机 append 冲突；节点宕机未封存的尾段由新 claim 者
  按 jsonl 本地文件 + manifest 做 reconcile（对应 codex read-repair：
  `codex-rs/rollout/src/recorder.rs` `list_threads_with_db_fallback`，460 行起、
  721 行 warn 后回落文件系统——机制说明见 §1.2"索引分层"行；抄它的原因：
  文件/对象是真相、索引可能落后，读路径必须能以真相源为准自愈）
- **读取**：SSE 重放 / 上下文重建只读最近段（本地热数据）；跨段历史经 manifest
  定位对象拉回
- **本地盘为缓存**：节点本地 jsonl 可按 LRU 清理，对象存储为准

**多副本写入模型（已确认，2026-08-28）**：生产环境**不依赖**"多个副本 append 同一个
完整 thread 文件"，每个 turn 的执行流程固定为五步：

1. Worker claim 当前 turn（`conversation_turns` lease/fencing 保证同一 thread 单写者，
   现状已具备）
2. 以 thread_id + manifest 定位本地热段：缓存命中直接续写；未命中且存在未封存段
   → 从对象存储拉回续写；无未封存段 → 新建段
3. 追加并逐行 flush
4. turn 终态后封存当前 turn 的 segment（此后不可变）
5. **先上传对象成功，再在同一事务写 PG manifest 行**（顺序铁律：反向会产生指向
   不存在对象的悬空 manifest；崩溃在两步之间产生孤儿对象，由新 claim 者 reconcile
   时重新登记或清理——机制见上方 read-repair 条目）

跨副本安全三支柱：turn 级 claim 单写者 + segment 封存后不可变 + manifest
`(thread_id, ordinal_start)` 唯一约束。

- 本地开发环境：对象存储层用本地目录模拟（与 `LocalMarkdownStore` 同模式，
  [backend/memory/storage](backend/memory/storage/)）

### 1.8 Retention 与归档

- 新增配置：`conversation_rollout_root`、`conversation_rollout_retention_days`、
  `conversation_rollout_segment_max_bytes`
- maintenance loop（[graph_worker.py](backend/conversation/worker/graph_worker.py)
  `run_maintenance_loop`，advisory lock 单实例）增加：
  - thread 删除（[thread_deletion.py](backend/conversation/services/thread_deletion.py)
    链路挂接）→ 段对象移入 `archived_threads/` 前缀（对应 codex `archived_sessions/`）
  - 超过 retention → 物理删除段对象 + manifest 行 + messages 索引行
- 用户删除合规：删除 = 删段对象 + 索引行，比现状（软删除标记散落多表）更简单可审计

### 1.9 记忆提交链路（Evidence Submission）的配套改造

**现状已经就是"引用提交"，与新方案同构**——agent 从不提交全文：

1. finalize 节点（同一事务）：写助手消息 → `build_source_manifest(thread_id, turn_id,
   message_rows)` 生成 `source_checkpoint_id`（对 `{thread_id, turn_id, 按 sequence 排序的
   message_id/role/sequence/content_hash}` 做 canonical JSON + 完整 SHA-256，§7.2/D9，
   [backend/conversation/contracts/domain.py](backend/conversation/contracts/domain.py)
   172–197 行）→ 写 `conversation_outbox` 行
   （[backend/conversation/graph/nodes/finalize.py](backend/conversation/graph/nodes/finalize.py)）
2. MEMORYACK 节点快速 claim outbox，短超时投递
   `submit_conversation_evidence(thread_id, message_ids, checkpoint_id, ...)`；
   失败由独立 Publisher 重投
   （[backend/conversation/graph/nodes/memory_ack.py](backend/conversation/graph/nodes/memory_ack.py)）
3. memory-api 收 `ConversationEvidence{thread_id, checkpoint_id, message_ids, trigger,
   topic_hints, graph_node_hints}`（[backend/memory/contracts/evidence.py](backend/memory/contracts/evidence.py)
   12–27 行）→ operation 队列
4. MemoryManagerGraph `load_source_refs` 经 Reader 边界回查正文：
   `ConversationReader.read(thread_id, checkpoint_id, message_ids)` →
   `HttpConversationReader` 打 conversation 内部 API 读 messages 表，外层
   `DeletionAwareConversationReader` 抑制已删除来源 → `SourceBundle`（≤80KB）→
   提炼写 Markdown（[backend/memory/graph/summary.py](backend/memory/graph/summary.py) 76–99 行）

**rollout 化后：提交载荷不变，引用解析目标变**：

| 元素 | 现状 | rollout 化后 |
|---|---|---|
| `thread_id` / `message_ids` | 指向 messages 表行 | 不变——UUID 是稳定键，与存储无关 |
| 正文解析路径 | Reader → conversation 内部 API → messages 表 | Reader → PG 索引（message_id → `(segment_object_key, byte_offset)`）→ 读 jsonl 段（热数据读本地，冷数据拉对象存储）；HTTP 内部 API 降级为备选 |
| `source_checkpoint_id` | manifest 输入来自 DB 行 | 算法不变（manifest 只含 id/role/sequence/content_hash，不含正文），content_hash 权威来源换成 jsonl 行；manifest 追加该 turn 的 ordinal 范围做冗余定位 |
| 删除合规 | `SourceDeletedEvent(source_ref=message_id)` + 软删除标记 | source_ref 不变；底层删除 = 删段对象 + 删索引行；DeletionAware Reader 语义不变 |
| outbox / MEMORYACK / Publisher | DB 协调 | 不动（协调层，不属于"PG 只存索引"要砍的内容） |

### 1.10 测试与验收

| 层级 | 内容 | 对标 |
|---|---|---|
| 单元 | policy 白名单、文件命名/解析、双时间戳格式、ordinal 单调 | `rollout_file_name_tests.rs`、`recorder_tests.rs` |
| 集成 | 写入 → kill -9 → 重放一致性；延迟建文件；flush ack 语义 | `rollout_reconstruction_tests.rs` |
| 崩溃注入 | kill -9 worker 后 jsonl 与 turn 终态一致率（第二阶段切 SSE 的验收门槛） | codex flush-per-line 语义 |
| 契约 | rollout 行 schema 进 `contracts/rollout.py`，快照测试 | 现有 tests/contract 机制 |
| 门禁 | `scripts/ci-local.sh` 全 stage | AGENTS.md |

### 1.11 开放问题（已决议，见文末决议总表）

1. checkpoint 瘦身（`snapshot.recent_messages` 从全文内联改为 message_id 引用）违背
   方案 §9.3"恢复时禁止重读 DB"，需单独评估，第一阶段不动。
   **→ 决议（C 组）：维持现状不动**。checkpoint 自包含是 LangGraph 恢复语义，
   本改造不碰；零工作量，确认即关闭。
2. `turn_context_snapshot` 行是否存全文：当前决策为摘要级 + 引用；若要求 jsonl 脱离
   DB 也能完整重放，需改为全文并接受体积膨胀。
   **→ 决议（C 组）：维持摘要级 + 引用**，不改全文。
3. 段封存触发条件的默认值（每 turn 一段 vs 大小阈值优先）待压测确定。
   **→ 决议（B 组）：每 turn 一段**（简单优先），大小阈值仅作防爆兜底
   （`conversation_rollout_segment_max_bytes`），压测校准后置。
4. 多副本部署下"同一 thread 追加同一文件"的写入模型：Worker 只保证同一 thread
   同时一个活动 turn，不代表同一进程长期持有文件句柄——是否接受"每 turn 定位
   热段 → append+flush → 终态封存 → 写 manifest"、不依赖共享 append 的方向。
   **→ 决议（B 组）：接受**。物理载体 = 每 turn 一个不可变 segment，完整 thread
   文件是 manifest 拼装的逻辑视图；五步流程与顺序铁律见 §1.7"多副本写入模型"。

---

## 第二章 长期记忆（总结记忆）格式与读路径改造

### 2.0 设计思路（一句话）

**内容全保留，动的是"索引密度"与"注入层级"**：保留现有 learner / mastery / index
三文档内容模型与版本化存储，新增 `memory_summary.md` 预注入层、把 `index.md` 升级为
带检索锚的可搜索注册表；读路径从"每轮 query 驱动注入"改为 codex 式的
**"首轮 prime 注入固定提示词 + 之后 agent 持记忆工具自助检索"**。

### 2.1 codex-rs 参考机制与抄代码位置

| 机制 | codex-rs 代码位置 | 要点 | 我们抄什么 |
|---|---|---|---|
| `memory_summary.md` 预注入 | `codex-rs/ext/memories/src/prompts.rs`、`templates/memories/read_path.md`（125–127 行） | 整文件渲染进 developer instructions，超 token 上限截断 | 新增每用户 `memory_summary.md`，首轮注入 |
| summary schema 版本与重置 | `codex-rs/memories/write/src/workspace.rs` `validate_consolidation_artifacts`（71–79 行） | 首行必须恰好是 `v1`，否则整体重生成 | 同款首行标记 + schema reset 校验 |
| summary 内容格式 | `codex-rs/memories/write/templates/memories/consolidation.md`（454 行起 "`memory_summary.md` FORMAT (STRICT)"） | User Profile ≤350 词 / User preferences / General Tips；高信号密度、激进去重 | 按我们的学习场景改写分区（见 §2.3） |
| `MEMORY.md` 注册表 | 同上模板（201–344 行 "`MEMORY.md` FORMAT (STRICT)"） | Task Group 块 + `### keywords` 判别性检索词 + `### rollout_summary_files` 路由锚；"easy to grep" | index.md 条目增加 keywords + 一句话 scope |
| 三层读取与预算 | `codex-rs/ext/memories/templates/memories/read_path.md`（19–46 行） | summary 已在提示词 → grep 注册表 → 命中才开下层文件；≤4–6 步搜索预算 | 记忆工具 + 调用上限 + prompt 引导词 |
| 记忆引用义务 | 同上（75–115 行 `<oai-mem-citation>`） | 用过记忆就在回答末尾附引用块 | 对齐到我们 citations 体系（可选，第二阶段） |
| ad_hoc 更新笔记 | 同上（117–123 行） | 用户显式要求"记住"时只写小笔记文件，不直接改记忆 | 与 `explicit_remember` 触发器语义契合（可选，第二阶段） |

**不抄清单**：`rollout_summaries/`（我们的 mastery/learner 文档本身就是摘要层，
evidence_refs 已是结构化路由，rollout 化后直达 jsonl 段，中间层纯冗余）、
`raw_memories.md`（阶段中间产物，我们的 SourceBundle 在图状态里不落盘）、
`skills/` 目录、`extensions/`（除 ad_hoc 外）、git baseline diff（我们有 versions 表，
可从版本历史生成等价 diff）。

### 2.2 现状盘点（保留部分）

| 已有资产 | 代码位置 | 处置 |
|---|---|---|
| 三文档 schema：learner-profile / mastery-profile / memory-index，front matter + 确定性渲染 + round-trip 解析 | [backend/memory/storage/markdown_schema.py](backend/memory/storage/markdown_schema.py) | **保留**（codex 的 MEMORY.md 是自由格式无校验，我们不做这个倒退） |
| 版本化存储：不可变版本 + current 物化 + quarantine + 原子写 + checksum | [backend/memory/storage/local_markdown.py](backend/memory/storage/local_markdown.py) | **保留**（codex 没有，是我们的强项） |
| `evidence_refs` → message_id 路由 | markdown_schema.py（证据引用节） | 保留；第一章 rollout 化后解析为 jsonl 段坐标 |
| PG 索引检索 `index_repo.search_candidates`（title/topic_key 相似度） | [backend/memory/services/context_service.py](backend/memory/services/context_service.py) | 保留并升级（加 keywords 列），变为 `memory_search` 工具的服务实现 |
| `LearningContextService.build`：每轮 query 检索 + 4 级 token 裁剪 | context_service.py 204–333 行 | **拆解**（见 §2.4）：组装/注入逻辑大部分退役 |
| conversation 域 `recall_memory` 节点：每轮一次 query seed 检索 | [backend/conversation/graph/nodes/memory.py](backend/conversation/graph/nodes/memory.py) | **改造**为首轮 prime / 非首轮短路 |

### 2.3 目标文件框架（每用户）

```text
users/<user_id>/
  memory_summary.md        ← 新增：预注入层
  current/learner.md       ← 保留（内容不变）
  current/mastery/<topic_key>.md
  current/index.md         ← 保留但升级为注册表（schema_version 2，待确认）
  versions/...             ← 保留（版本历史不变）
```

**`memory_summary.md` schema（v1）**：

```text
v1
（首行必须恰好是 v1，无 front matter；校验不符 → 从 MEMORY 层整体重生成，
  抄 codex validate_consolidation_artifacts 语义）

## 用户画像
（从 learner 文档投影，≤350 词：稳定的学习偏好、目标、沟通习惯；
 保守推断，一次性印象不落地——对齐 codex 的画像保守性规则，出处：
 `codex-rs/memories/write/templates/memories/consolidation.md` 508 行
 "This entire section is free-form, <= 350 words" 及同节保守推断约束）

## 稳定偏好
（跨主题、会改变未来行为的可执行偏好，短 bullet，激进去重）

## 主题路由
（每行一条：<topic_key> | <一句话掌握状态> | 熟练度 → mastery:<topic_key>；
 只作路由，不含正文细节）
```

**`index.md` 升级为注册表**（schema_version 2，待确认）：条目从单行
`memory_id | title | v{n} | updated_at` 扩展为带 **`keywords`**（判别性检索词：
概念名、术语、常见错误模式——抄"未来 grep 会用的词"原则）与**一句话 scope** 的块结构；
仍保持 front matter + 确定性渲染 + round-trip 解析 + 版本化。
PG 索引表同步加 keywords 列，检索召回升级。

### 2.4 读路径改造（本章核心，含已拍板决策）

**D1 首轮 prime 注入，之后不再每轮注入**

- 首轮判定：thread 无历史消息（查 messages 索引表即可，不新增标记位；
  我们的 graph thread 是 `conv-turn:{turn_id}` per-turn，注入状态不能放 Graph State）
- 首轮注入内容：`memory_summary.md` 全文（截断到独立小预算）+ **记忆索引目录**
  （注册表条目：memory_id / title / scope / keywords，**不含正文**）
- `recall_memory` 节点改造：首轮 → prime 注入；非首轮 → 直接短路。
  消掉 §9.3 #6"补检索循环禁止再读 Memory"的约束（该约束由新工具语义取代）
- 新增轻量 `build_memory_prime()`：读 summary 文件 + 索引表，无检索打分

**D2 固定提示词防跨轮遗忘（用户拍板）**

- 首轮注入的内容视为**提示词的一部分并固化（pin）**：作为不可变快照随该 thread 的
  持久载体保存（第一章 rollout 化后落 `memory_prime` 行 + checkpoint 快照）
- **每次上下文压缩后，将同一份固定提示词原样重注入**——压缩丢失的是对话历史，
  不是记忆提示词
- **记忆更新不回溯旧提示词**：旧 thread 保持旧快照继续用；新 thread 的 prime
  自然用最新 summary。旧提示词的"过期"是可接受语义（对应 codex session 内
  summary 不热更新，出处：`codex-rs/ext/memories/src/prompts.rs`
  `build_memory_tool_developer_instructions` 在会话启动时一次性读取
  memory_summary 并渲染进 developer instructions——`ext/memories/src/extension.rs`
  19 行注册调用，运行期间不重读文件；抄它的原因：避免同一 session 内提示词
  漂移导致的上下文不一致，代价（旧快照过期）由新 thread 自然吸收）

**D3 记忆工具集 v1：answer 路径 tool-call 循环自助取记忆（用户拍板定稿）**

参考 codex-rs 记忆工具（[ext/memories/src/tools](codex-rs/ext/memories/src/tools/mod.rs)，
namespace `memory` 下 list/read/search/add_ad_hoc_note 四个，文件系统视角），
我们收敛为**两个工具**，不要 list：

- 首轮 prime 已直注 index 注册表目录（D1）；`memory.search` 查的是**实时 index 表**
  而非注入的旧快照——注入的目录只是提示，search 才是事实源，pin 住的旧目录过期
  不影响正确性。list 的两个用途（浏览目录/发现新文件）被首轮注入和 search 全覆盖

**① `memory.search`：纯关键词，可预测（用户拍板，不引入向量）**

- 入参：`queries: string[]`、`match_mode: any|all`（对齐 codex SearchArgs 子集）、
  `max_results`（clamp 上限）
- 匹配域：index 表的 `name / description / aliases / keywords` 四列（ILIKE 子串），
  **不碰向量、不碰正文**——正文由 agent 先 search 定位、再 read 下沉
- 出参：注册表条目（memory_id / name / description / keywords / version /
  updated_at），**不含正文**，强制"search 定位 → read 下沉"两段式
- 排序固定规则（不用模型打分）：updated_at 近者优先 + 命中列权重
  （name/aliases > keywords > description）

**② `memory.read`：分段 + 预算 + 溯源**

- 入参：`memory_id`、`line_offset?`、`max_lines?`（对齐 codex 的 1-indexed 分段读，
  [read.rs](codex-rs/ext/memories/src/tools/read.rs)）
- 出参：正文片段 + `version`（供引用）+ `truncated` 标记；走版本化读取 +
  删除抑制（DeletionAware 语义）；单文档计入现有 `memory_context_token_budget`
  （预算沿用旧体系，不新设预算族）

**③ tool-call 循环 + 流式交错（现在就改链路，用户拍板不做绕开）**

- answer 节点与新增 `memory_tool` 节点构成循环边：模型流式生成中可以发起
  工具调用 → 图执行工具 → 结果回灌 → 继续流式生成
- **SSE 协议同步扩展（现在改）**：新增工具调用事件类型（`memory.tool_call` /
  `memory.tool_result` 或统一 `turn.tool_activity` 状态事件），前端可展示
  "agent 正在查记忆"；事件仍走 TurnEventWriter 同一事务追加（§7.4 机制不变）
- 防死循环：记忆工具调用次数上限（对应 codex ≤4–6 步预算，出处：
  `codex-rs/ext/memories/templates/memories/read_path.md` 43–46 行
  "Quick-pass budget: ideally <= 4-6 search steps before main work"），超限记
  `degraded_flags` 继续回答；工具失败沿用 §16.2 降级语义
- 工具调用与结果按第一章 policy 落 rollout jsonl（白名单新增 `memory_tool_call` 类型）
- answer prompt 增加 quick memory pass 引导词（对齐 codex read_path.md：
  "问题涉及用户学习历史/偏好/先前掌握 → 先 search 再答；不确定就先查一次；
  重复报错/疑似有先前上下文时重查"）

**④ 旧格局退役清单**

- `recall_memory` 节点：非首轮短路（D1 已定）；`memory_read` flag 语义改为
  "记忆工具是否可用"
- `MemoryGateway.build_learning_context` / `LearningContextService.build` 的
  query 检索 + 4 级裁剪 + 组装注入：退役；只保留单文档预算裁剪给 `memory.read`；
  新增轻量 `build_memory_prime()`（读 summary + index 目录，无检索）供首轮 prime
- 规格 §16 相关章节随实施同步改写

### 2.5 教学场景适配说明

我们是数学教材学习场景，学生提问主题跳跃大："每轮按 query 猜该注入什么"本质是
猜测，新模式是**按需精确读取**（讲到椭圆才读 `mastery:椭圆`），token 效率与准确率
都更好。首轮注入的目录本身即提示；回答的 citations 体系可扩展"记忆引用"
（对齐 codex citation 义务，第二阶段）。

### 2.6 总结时机：证据池 + 每日批量调度（用户已拍板）

**设计思路**：学习 codex-rs"提交与处理解耦 + 有界批量认领 + 两段式（逐条提取 →
统一合并）"的机制内核，但把它的触发点（会话启动时批量，[start.rs](codex-rs/memories/write/src/start.rs)
+ [phase1.rs](codex-rs/memories/write/src/phase1.rs) 149–182 行的有界 claim）替换为
服务端定时调度——**每条证据至少沉淀 N 小时才进入总结，总结整理固定在每日 0 点批量执行**，
避免单条提交即时处理过于分散。

**已拍板决策**：

- **D4 参数化配置**（不写死）：新增 settings
  - `memory_evidence_min_age_hours`（默认 6）：证据最短沉淀时长
  - `memory_summary_daily_time`（默认 `00:00`，走 Scheduler 既有
    `memory_scheduler_timezone`，默认 Asia/Shanghai）
  - `memory_summary_batch_max_evidence` / `memory_summary_max_users_per_run` /
    `memory_summary_llm_concurrency`：有界认领三件套（对齐 codex 的
    `max_rollouts_per_startup`（`codex-rs/memories/write/src/phase1.rs` 170 行
    的 claim 参数，配置定义在 `codex-rs/config/src/types.rs` 310 行）/
    `CONCURRENCY_LIMIT` / `JOB_LEASE_SECONDS`（`codex-rs/memories/write/src/lib.rs`
    81–82 行 stage_one 常量：并发 8、租约 3600 秒；抄它的原因：批量任务必须
    有界，防止一次启动把全部待办打满 LLM 并发与 worker 租约）
- **D5 explicit_remember 豁免**：用户显式"记住这个"不受最短沉淀时长约束，
  下一个 0 点批即处理（MEMORYACK 的快速 ACK 语义不变，用户立即得到"已收到"）
- **D6 批量粒度：每用户一批**（不按主题拆分）

**证据池建模（已决议：方案 A 修订版——复用 memory_operations，不建新表、不加 eligible_at 列）**：

- 候选方案：A 复用 `memory_operations` 加资格门控与批次归属 / B 新增独立
  `memory_evidence` inbox 表。**定稿 A 修订版**，依据（代码核实）：
  - `memory_operations.next_run_at` 列**现成**（[0001_memory_core.py](alembic/versions/0001_memory_core.py)
    78 行），且共享认领查询 `claim_operation` 已过滤 `next_run_at <= now()`
    （[persistence/operations.py](backend/memory/persistence/operations.py)
    107、117 行）——最短沉淀门控 = 提交时设置 `next_run_at`，**零新列**
  - 但只靠 `next_run_at` 有洞：到期后普通 Worker / Gateway P0 快速路径会把证据
    单独领走立即执行，批量失效。解法：**新增状态值 `pending_batch`**——
    claim 只认 `('queued','retry_wait')`，`pending_batch` 对 Worker/Gateway
    天然不可见，共享 claim 查询不用改
  - 不选 B 的原因：B 需复制整套 operation 信封机制（幂等键、lease/fencing、
    retry/dead_letter、metrics、[account_purge.py](backend/memory/services/account_purge.py)
    级联），并引入两套状态机对账；"提交与处理解耦"用 `pending_batch` 即可
    等价获得。codex 对应物也是单表 jobs 扫描认领（`memories/write/src/phase1.rs`），
    无独立 inbox 层
- **新增列**：`batch_operation_id uuid NULL REFERENCES memory_operations(operation_id)`
  （证据 → 批次归属，单 FK 足够：一个证据只进一个批，消费后即终态）
- **状态机**：
  1. 提交：evidence operation 落库为 `pending_batch`，
     `next_run_at = submitted_at + memory_evidence_min_age_hours`
     （`explicit_remember` 豁免：`next_run_at` = 下一个 0 点）
  2. 0 点入批：Scheduler 扫 `status='pending_batch' AND next_run_at <= now()`，
     按 user_id 分组生成批量 op，成员行写 `batch_operation_id`（状态不动）
  3. 批量 op `succeeded` → 同一事务把成员 evidence 置 `succeeded`
  4. 批量 op 失败走现有 `retry_wait`/lease 机制，期间成员保持 `pending_batch`；
     批量 op `dead_letter` → 成员一并置 `dead_letter`（带批次归属）进人工审
- **实施影响面**：`contracts/common.py` `OperationStatus` Literal + DB CHECK
  约束迁移 + OpenAPI 快照更新；`account_purge` 活跃状态列表加 `pending_batch`；
  metrics 增加 pending_batch 在途 gauge

**提交侧（改动小）**：

- conversation → memory-api 链路不动（outbox / MEMORYACK / Publisher 保留）
- memory-api 收到 `conversation_evidence` 后不再立即生成可执行 operation：
  按上述状态机落 `pending_batch` 行

**处理侧（0 点批量）**：Scheduler（[backend/memory/worker/scheduler.py](backend/memory/worker/scheduler.py)，
已有 daily_at / advisory lock / maintenance_runs 幂等 / batch cursor + continuation 续跑）
新增日任务 `summarize_pending_evidence`，`daily_at=00:00`：

1. 选取：扫 `status='pending_batch' AND next_run_at <= now()` 的证据行，
   按 user_id 分组
2. 每用户生成一个批量 operation（新类型 `summarize_user_memory_batch`，payload 携带
   该用户本批全部证据引用），幂等键 `summarize:{user_id}:{date}` 走现有
   maintenance_runs 机制
3. 有界执行：单批证据上限 + 单 run 用户数上限 + LLM 并发上限；跑不完用
   continuation cursor 续跑（机制现成）
4. MemoryManagerGraph 内两段式：先逐证据提取，再统一合并写 learner/mastery——
   **同时回答了 §2.7 遗留问题 2：`memory_summary.md` 由批量总结的合并段整体重写
   （consolidation 式），不做增量投影**

**已识别的代价与对策**：

- 记忆新鲜度：最长延迟约 30 小时（23:30 提交 → 后天 0 点）。对策即 D5 豁免通道；
  常规证据接受隔日可见
- 0 点峰值：有界认领 + continuation 续跑把 LLM 负载摊到 0 点后；0 点与既有
  02:30–05:00 维护任务天然错开

### 2.7 开放问题（已决议，见文末决议总表）

1. `index.md` schema_version 2：条目改块结构后，现有 `_INDEX_ITEM_PATTERN` 单行正则
   需改块解析——是否接受 index 格式升版。
   **→ 决议（A 组）：接受升版**。index 块解析 + 文档 frontmatter v2
   （name/description/aliases）+ `[[link]]` 解析合并为一次 schema_version 2 升版
   （与 §3.7-⑤ 同一件事）。
2. ~~`memory_summary.md` 生成方~~ **已由 §2.6 解决**：批量总结的合并段整体重写
   （consolidation 式），不做增量投影。
3. 记忆工具调用上限与现有 `_max_retrieval_iterations` 补检索预算是否共享。
   **→ 决议（B 组）：独立预算，不共享**（检索是质量兜底，记忆是按需读取，
   共享会互相挤占），默认 6 次/轮。
4. ad_hoc 更新笔记（explicit_remember 语义对齐）与记忆 citation 块，是否进第二阶段。
   **→ 决议（F 组）：明确后置第二阶段**，不进本轮实施范围。
5. schema_version 2 的迁移策略：渐进（解析器双读 v1/v2 + 后台逐用户升级）vs
   一次性迁移命令。
   **→ 决议（A 组）：渐进迁移**。一次性迁移在现有存储语义下不可行：`versions/` 是
   不可变版本、文件名内嵌 checksum12（[storage/base.py](backend/memory/storage/base.py)
   29–43 行），重写历史版本会让 checksum 级联失效、`verify_checksums` 全线告警。
   具体策略：**历史版本永不迁移**（读历史按各自 frontmatter 的 schema_version 分派
   解析器，双读是长期能力）；"迁移"= 后台维护任务把 current/ 按 v2 重新渲染并走正常
   `write_immutable_version` 追加新版本（version+1），版本历史与回滚语义不受损；
   载体 = Scheduler daily 任务表 + maintenance_runs 幂等 + batch cursor
   （[scheduler.py](backend/memory/worker/scheduler.py) 84–91 行），新增
   `migrate_markdown_schema_v2` 逐用户升版。codex 同思路佐证：schema 不符整体重生成
   而非原地改写（§2.1 表"summary schema 版本与重置"行）。

---

## 第三章 文档层关联性改造与 KG 关系重定位（OPUS-5 式）

### 3.0 设计思路（一句话）

对文档层（learner/mastery）做 OPUS-5 式改造（frontmatter 规范 + `[[link]]` 互链 +
aliases 归并）；注册表层（index）仍是"改造后文档的索引目录"；**KG registry + overlay
是另一套独立记忆体系，与长期记忆的关联只在"更新"上同源联动，注入时分别注入，
冲突协调放在 context build 阶段**。

### 3.1 OPUS-5 参考机制与位置

| 机制 | OPUS-5.md 位置 | 要点 | 我们抄什么 |
|---|---|---|---|
| 文件结构 | 238–249 行 | frontmatter（name/description/sources/aliases）+ 事实行 | mastery/learner frontmatter 扩展（schema_version 2） |
| `[[links]]` 互链 | 253、261–265 行 | name（path stem）全局唯一 = 链接解析键；事实涉及另一主体时 `[[name]]` 互链；**悬空链接合法**，标记"值得日后建档" | 文档正文引入 `[[link]]`，解析键 = memory_id/aliases；悬空链接喂 nightly 批 |
| taxonomy 铁律 | 329–336 行 | 一个事实只进其主体的文件，关联全部用链接表达，文档间不复制内容 | 照搬为 mastery 写作纪律 |
| aliases 实体归并 | 316–327 行 | 同一主体的不同叫法收敛到一个文件，防重复建档 | mastery frontmatter 新增 aliases |
| `<memory_listing>` 目录 | 186–196、1676–1679 行 | 每轮注入：path + 一行 description + aliases + sources；"给提示不给内容" | 已由第二章 D1 对齐（首轮注入注册表目录），本章补 description/aliases 投影规则 |
| 事实溯源纪律 | 267–310 行 | 只落用户直接陈述的事实（`[stated]`）；agent 推断/建议/展望不进文件 | 语义沿用（我们不照搬标签）：正文事实必须挂 evidence_refs，agent 推断不进正文 |

### 3.2 文档层 schema v2

**frontmatter（与现有字段合并，版本化/quarantine/原子写全部保留）**：

```yaml
---
kind: mastery-profile            # 保留
schema_version: 2                # 升版（v1 → v2：新增 name/description/aliases）
memory_id: "mastery:椭圆"         # 保留，全局唯一 = [[link]] 解析键
name: "椭圆"                      # 新增：链接显示名
description: "..."               # 新增：一行描述，注册表目录展示，"要不要打开它"的判据
aliases: ["ellipse", "椭圆形"]    # 新增：实体归并
topic_key / topic_title          # 保留但语义改为长期记忆自有命名空间（见 §3.3）
version / updated_at / confidence / evidence_count   # 保留
---
```

**正文 `[[link]]` 规则**：

- 语法 `[[name]]` 或 `[[memory_id]]`；解析顺序：memory_id 精确 → name → aliases
- 关联铁律：一个事实只进其主体的 mastery 文档；跨主题关系只用链接表达
  （例：`mastery:椭圆` 的"仍有困难"写"与 [[抛物线]] 的焦点性质混淆"，
  不复制抛物线内容）；learner 同样可链（"当前计划"里 `[[mastery:导数]]`）
- **悬空链接合法**：指向不存在的主题；每日 0 点批量总结（§2.6）收集全部悬空链接，
  作为"候选新 mastery 主题"输入 consolidation——OPUS "dangling link flags something
  worth filing later" 的服务端落地
- round-trip 解析器（markdown_schema.py）扩展：渲染/解析识别 `[[...]]`，
  链接不存在的告警进一致性维护（不阻断写入）

### 3.3 mastery 主题命名空间独立（用户拍板）

- mastery 主题**是长期记忆自己的命名空间**，与图谱本体脱钩：
  拆除 [local_markdown.py](backend/memory/storage/local_markdown.py) 33–40 行
  `validate_existing_topic_key` 对 KG registry 的强校验
- 主题治理改用 OPUS 模式：**自由建档 + aliases 归并 + nightly 批清理**
  （出处：OPUS-5.md 329–379 行 "Where it goes" 节——按主题一文件、359 行
  "create food.md, don't append to hobbies"、379 行起同名归并进 `aliases:`；
  抄它的原因：主题集合是开放世界，注册表强校验会拒绝新知识，自由建档 +
  事后归并才能跟上学习者的真实提问分布。consolidation 时发现近义主题文件，
  按 aliases 合并并在 index 中留归并记录）
- learner 维持单文件（等价 OPUS 的 /profile.md + /preferences.md 合并，不拆分）

### 3.4 注册表（index）投影规则

- 索引项对齐改造后文档：`memory_id | name | description | aliases | keywords |
  version | updated_at`；description / aliases 从文档 frontmatter **投影**而来
- **文档是唯一事实源，index 是可再生派生物**（rebuild_index 维护链路现有，
  [scheduler.py](backend/memory/worker/scheduler.py) `schedule_index_rebuilds`）
- 首轮注入目录（第二章 D1）注入此列表，对齐 OPUS "listing 给提示不给内容"

### 3.5 KG 关系重定位（用户拍板）

**KG registry + overlay 是另一套独立记忆体系，不进本文档的格式改造范围。
两套体系的关系只有两处接触面：**

**① 写路径：同一证据、双路更新（唯一的关联点）**

- 例：对话表明"用户对微积分很熟悉" → 长期记忆写入/更新 `mastery:微积分`，
  图谱侧同步更新对应节点状态
- 现有契约雏形：`GraphProjectionEvidence`（direction: learning / positive /
  strong_positive / conflict，[backend/memory/contracts/evidence.py](backend/memory/contracts/evidence.py)
  157–163 行）——挂在 §2.6 nightly 批量总结里做双路分发：consolidation 产出
  长期记忆变更的同时产出图谱投影证据
- 两路更新**不追求事务一致**（两套独立体系），各自幂等；冲突留给读路径协调

**② 读路径：分别注入 + context build 冲突协调**

- 长期记忆走长期记忆的注入（第二章：首轮 prime + 记忆工具）；图谱走图谱自己的
  注入通道——两边分别进 context，互不代理
- **冲突协调是 context build 阶段的工作**：组装注入前做两套记忆的一致性检查
  （例：长期记忆"熟悉微积分" vs 图谱节点状态"薄弱" → 裁决策略：更新鲜者优先 /
  证据强度优先 / 并列呈现并标注冲突——具体策略待讨论）
- 落点：`LearningContextService` 的 graph_states 注入段
  （[backend/memory/services/context_service.py](backend/memory/services/context_service.py)
  279–314 行）从"弱连接展示"改造为"双源协调点"

### 3.6 记忆总结 Agent 提示词配套改造

格式契约必须配提示词，否则 agent 不知道怎么写。现有两段式提示词管线
（[backend/memory/graph/prompt_loader.py](backend/memory/graph/prompt_loader.py) 版本管理，
每次 LLM 调用记录 prompt_version）配套升版：

**① `build_mutation_plan` v1 → v2**（主战场，[现版](backend/memory/graph/prompts/build_mutation_plan_v1.md)）：

新增教学规则，来源为 OPUS-5.md 238–336 行的本地化改写：

- frontmatter v2 维护：`create` 时必须生成 `name` / `description`（一行、供注册表
  目录展示，回答"这个文件里有什么"）/ `aliases`；`merge` 时按需更新
- `[[link]]` 语法 + 关联铁律：**一个事实只进其主体的文档**；候选涉及其他主题时
  用 `[[主题名]]` 互链，禁止把别的主题内容复制进本文档
- 悬空链接允许（不存在的主题照常链接，由 nightly 批收集）
- 事实溯源纪律强化（对齐 OPUS `[stated]`）：只落用户直接表现证实的事实；
  现有"不得把助手讲解当作用户掌握事实写入"保留并扩展到 learner
- 配套代码变更：MutationPlan schema 扩展 `frontmatter_patch` 动作
  （description/aliases 可补丁），planner 提示词与新动作一起上线

**② `extract_candidates` v2 → v3**（小改，[现版](backend/memory/graph/prompts/extract_candidates_v2.md)）：

- 候选增加**主体归属**字段（该事实属于哪个主题/learner），作为 taxonomy 铁律的
  路由依据
- 增加 `related_topic_hints`（候选涉及的相邻主题），供 planner 生成 `[[link]]`

**③ 新增 `summary_consolidate_v1`**（第二章 `memory_summary.md` 的生成提示词）：

- 教 consolidation 段从 learner + mastery 投影生成 §2.3 的 v1 格式：首行 `v1`、
  用户画像 ≤350 词（保守推断）、稳定偏好（可执行 bullet、激进去重）、
  主题路由（一行一条只作路由）
- 素材输入 = 当前全部长期记忆文档 + 本批变更 diff（对齐 codex phase2 的
  workspace diff 输入形态：`codex-rs/memories/write/src/workspace.rs`
  `write_workspace_diff`（34 行）生成 `phase2_workspace_diff.md` 供
  consolidation agent 读取，上限 `MAX_BYTES = 4MB`（lib.rs 113 行）；
  抄"diff 作为变更输入"的形态——让 agent 聚焦本批变化而非全量重读；
  但我们从 versions 表生成等价 diff，不抄 git baseline）

**④ 版本绑定与兜底**：

- prompt 版本与文档 schema 绑定：schema v2 文档必须配 v2 planner prompt，
  混用视为配置错误（启动校验）
- prompt 教写、validator 兜底：round-trip 解析器（markdown_schema.py）检查
  frontmatter 必填、`[[link]]` 可解析、description 单行；失败进一致性维护告警，
  不阻断写入（沿用现有 checksum/告警链路）

### 3.7 开放问题（已决议，见文末决议总表）

1. context build 冲突裁决策略（新鲜度优先 / 证据强度优先 / 并列标注）未定。
   **→ 决议（D 组）：更新鲜者优先 + 并列标注**——按两边时间戳取新的一方为准，
   同时在注入内容中标注"与另一来源存在冲突"，模型知情，不作静默覆盖。
2. aliases 归并的执行者：consolidation agent 自动合并 vs 人工确认。
   **→ 决议（E 组）：自动合并**。归并记录写 index + 版本历史可回滚，
   保留人工纠错通道；人工确认会破坏 nightly 批的全自动性。
3. 悬空链接提升为新主题的阈值（出现 1 次即建 vs 累计 N 次）。
   **→ 决议（E 组）：两级制**——出现 1 次即列为"候选主题"（只进 index，
   不建 mastery 文档）；累计 ≥2 批出现才正式建档（防一次性噪声成档）。
4. KG 侧更新失败的重试与对账（双路分发无事务，需要 reconciler 还是接受最终一致）。
   **→ 决议（D 组）：接受最终一致 + 告警**，不建 reconciler——双路各自幂等重试，
   差异由每日 verify 类任务发现并告警。
5. index.md schema_version 2（§2.7 遗留 1）与本章文档 frontmatter v2 合并升版。
   **→ 决议（A 组）：合并为一次 schema_version 2 升版**（同 §2.7-①）。

---

## 第四章 nightly 批量模式的图执行结构（方案 A，已拍板）

### 4.0 设计思路（一句话）

**现有节点一个不换，图内加两样**：① 证据循环段（对批量 operation 内该用户的 N 条
证据逐条走现有 extract → plan → commit）；② 循环结束后的 consolidation 末段。
checkpoint 机制原样保留，不改。

### 4.1 为什么不改 checkpoint

memory 域 checkpoint 现状：每 operation 一个 graph thread
（`thread_id_for_operation`），AsyncPostgresSaver 存 memory 库，崩溃从最近
superstep 恢复，终态由每日 03:30 `cleanup_checkpoints` 清理
（[scheduler.py](backend/memory/worker/scheduler.py) 345–381 行）。

MemoryManagerGraph 是内部处理引擎，其工作态没有"人类可读、可重放"需求
（那是 conversation 短期记忆的问题，第一章已解决）；在三层模型下它本来就已经是
"恢复缓存"角色。**结论：原样保留**。且图内循环的每次迭代都是 superstep，
批量中途崩溃从循环中间恢复，已处理的证据不丢——checkpoint 天然支持批量恢复。

### 4.2 图结构调整

现状粒度：一条证据 = 一个 operation = 跑一次图
（load_source → extract_candidates → build_mutation_plan → commit）。
nightly 批量后（§2.6 D6）：一个批量 operation 装该用户积压的 N 条证据。

```text
批量 operation（summarize_user_memory_batch，每用户一个）
  └─ MemoryManagerGraph 一次运行
       ├─ 循环段（每条证据一次迭代，均为可恢复 superstep）：
       │    load_source → extract_candidates → build_mutation_plan → commit
       │    （节点复用现有实现；load_source_refs 从单 payload 改为按批内证据逐条读取，
       │     现实现一次只读一个 payload：backend/memory/graph/summary.py 76–99 行）
       └─ consolidation 末段（循环全部完成后执行，新增节点）：
            ① 重写 memory_summary.md（§2.3 v1 格式，prompt = summary_consolidate_v1，§3.6③）
            ② 悬空链接收集 + aliases 归并 + 近义主题合并（§3.2 / §3.3）
            ③ 批内冲突裁决（同一批互相矛盾的候选统一裁决一次，只写最终结论）
```

### 4.3 为什么必须加 consolidation 末段（而非复用现有改写节点）

现有改写节点的输入只有"本次证据 + 目标文档当前内容"（局部增量视角，
[build_mutation_plan_v1.md](backend/memory/graph/prompts/build_mutation_plan_v1.md)）；
末段三件事全是**该用户全文档**视角，单证据处理看不到全局：

| 末段职责 | 现有节点做不了的原因 |
|---|---|
| 重写 memory_summary.md | summary 是 learner + 全部 mastery 的投影，需通读该用户所有文档 |
| 悬空链接 / aliases / 主题归并 | 需扫全部 mastery 文档才能发现悬空链接与近义重复主题 |
| 批内冲突裁决 | 逐条独立 commit 会让矛盾证据先后互相覆盖；末段统一裁决只写最终结论 |

对照 codex phase1/phase2 拆分（[start.rs](codex-rs/memories/write/src/start.rs)
77–80 行）：phase1 逐 rollout 提取 ≈ 我们的循环段；phase2 consolidation ≈ 末段。
拆两段的原因相同：单遍处理保证不了全局一致性。

### 4.4 边界与语义维持

- **per-user 隔离**：批量 operation、consolidation、summary/链接治理全部按 user_id
  作用域，不跨用户；跨用户共享的只有基础设施（operation 队列 / Scheduler / worker）
- **KG 语义维持现状**（已确认）：本体共享只读 + 每用户独立 overlay；
  长期记忆 mastery 与该用户 overlay 状态按 §3.5 双路更新、context build 冲突协调
- 末段失败语义：循环段已 commit 的文档变更不回滚；末段整体可重试
  （幂等键 = operation 幂等键 + ":consolidation"），失败告警不阻塞次日批次

### 4.5 开放问题（已决议，见文末决议总表）

1. 循环段单批证据上限（§2.6 `memory_summary_batch_max_evidence`）与图内循环
   超时的关系：超长批次是否需要图内分批 commit。
   **→ 决议（B 组）：单批上限默认 50 条**；图内循环靠 checkpoint superstep
   恢复，**不需要图内分批 commit**。
2. consolidation 末段的 LLM 调用预算（全文档通读的 token 上限）与降级策略
   （超限时 summary 重写退化为"仅更新主题路由段"？）。
   **→ 决议（B 组）：超限降级 = summary 只重写"主题路由"段**，
   用户画像/稳定偏好保持旧版。

---

## 开放问题决议总表

> 覆盖四章全部 15 条开放问题（含 1 条此前已解决、1 条跨章重复），按本质融合为
> 6 组。原始问题文本保留在各章末尾，本节为唯一决议索引。

| 组 | 覆盖条目 | 决议 | 优先级 |
|---|---|---|---|
| **A. Markdown schema 升版** | §2.7-①、§2.7-⑤、§3.7-⑤ | 接受升版：index 块解析 + frontmatter v2（name/description/aliases）+ `[[link]]` 解析，合并为一次 schema_version 2；迁移策略 = **渐进**（解析器长期双读 v1/v2，历史版本不动，后台维护任务逐用户以"追加新版本"方式升版 current/） | **P0 实施前置** |
| **B. 有界性参数族** | §1.11-③、§1.11-④、§2.7-③、§4.5-①、§4.5-② | 段封存=每 turn 一段（大小阈值仅兜底）；多副本写入=每 turn 定位热段→append→封存→写 manifest，不依赖共享 append 句柄（§1.7 五步流程）；记忆工具上限独立 6 次/轮；单批证据默认 50 条且不分批 commit；末段超限降级为只重写主题路由段。默认值先定，压测校准后置 | **P0 实施前置** |
| **C. 全文 vs 引用** | §1.11-①、§1.11-② | 均维持现状：checkpoint 保持自包含全文内联不动；rollout 的 turn_context_snapshot 行保持摘要级 + 引用。确认即关闭，零工作量 | P2 确认关闭 |
| **D. KG 协同一致性** | §3.7-①、§3.7-④ | 冲突裁决 = 更新鲜者优先 + 并列标注（不静默覆盖）；KG 更新失败 = 最终一致 + 告警，不建 reconciler | **P1 实施中需要**（读路径协调点实现前） |
| **E. consolidation 自治边界** | §3.7-②、§3.7-③ | aliases 自动合并（版本历史可回滚 + 人工纠错通道）；悬空链接两级制：1 次列为候选主题（只进 index），≥2 批正式建档 | **P1 实施中需要**（末段实现前） |
| **F. 第二阶段功能** | §2.7-④ | ad_hoc 更新笔记 + 记忆 citation 块，后置第二阶段 | P2 后置 |

（§2.7-② 此前已由 §2.6 解决：summary 由批量总结合并段整体重写。）

---

## 后续章节规划（占位）

- （暂无；后续议题由讨论产生后追加）
