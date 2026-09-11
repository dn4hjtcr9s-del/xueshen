# memory-rebuild 实施偏差与补充登记

> 本文件记录 **实施过程** 中出现的、与设计文档 `memory-rebuild.md` 不一致或文档中
> 根本不存在的内容。目的是让 review 者不必逐行比对代码与文档，即可看清"哪些地方
> 是按文档做的、哪些地方是文档没写的、哪些地方偏离了文档以及为什么"。
>
> 维护约定（用户 2026-09-10 明确要求）：
> - 边开发边追加，**不得**事后一次性补齐；
> - 每个 Phase 汇报时必须同步给出该 Phase 的新增条目；
> - 只记录"与文档不符"和"文档没有"两类，**正常按文档实现的不记录**（否则本文件
>   会退化成第二份文档，失去检索价值）。
>
> 条目编号：`DEV-` 偏差（与文档冲突）、`ADD-` 补充（文档未规定/未提及）、
> `OPEN-` 待决（尚未定论或刻意延后）。编号一旦分配不再复用。

---

## 图例

| 列 | 含义 |
|---|---|
| 文档位置 | `memory-rebuild.md` 的章节；"无"表示文档完全未提 |
| 实现 | 代码里实际怎么做的 |
| 依据 | 为什么这样定（用户决策 / 代码事实 / 外部约束） |
| 影响 | 对后续 Phase 或 review 的影响 |

---

## Phase 0：契约、配置与数据库迁移

### DEV-001 rollout 对象存储配置未新建 `conversation_rollout_qiniu_*`

| 项 | 内容 |
|---|---|
| 文档位置 | §5.2 B 配置表（列出 `conversation_rollout_qiniu_bucket` / `_region` / `_domain` / `_access_key` / `_secret_key`） |
| 实现 | **不新增**这 5 项，改为复用 community 域既有的 `kodo_access_key` / `kodo_secret_key` / `kodo_bucket` / `kodo_region` / `kodo_cdn_domain` |
| 依据 | 用户决策（2026-09-10）：仓库已有可用的 community Kodo 适配器（`backend/community/storage/kodo.py`）与完整配置，不再新增第二套凭据 |
| 影响 | rollout 与社区图片**共用 bucket**，以 object key 前缀 `rollouts/` 隔离；生命周期规则须按前缀分别配置。Phase 3 的 `QiniuKodoRolloutObjectStore` 应复用 `kodo_*` 与既有 `KodoStorage` 的构造方式，不得假设存在 `conversation_rollout_qiniu_*` |

### DEV-002 `conversation_rollout_object_store` 取值与文档不符

| 项 | 内容 |
|---|---|
| 文档位置 | §5.2 B（`local` 或 `qiniu`） |
| 实现 | `Literal["local", "kodo"]` |
| 依据 | DEV-001 的连带结果：既然复用 `kodo_*`，取值域应与 `community_storage_backend: Literal["local","kodo"]` 对齐，否则出现 `qiniu` 模式读 `kodo_*` 配置的认知错位 |
| 影响 | 部署配置与文档 §5.2 B 表格字面不一致；.env.example 已按实现写 |

### ADD-001 rollout 段状态机取值文档未定义

| 项 | 内容 |
|---|---|
| 文档位置 | §5.2 C-1 只列出 `status` 列存在，**未给取值**；§5.4 步骤 4→5 暗示存在"未 sealed"状态；§1.7 步骤 2 又要求能用 manifest 定位"本地热段" |
| 实现 | 三段式 `open → sealed → deleted`。`open` 允许 `object_key`/`object_etag`/`sha256`/`byte_size`/`ordinal_end` 为空；`sealed` 时五者全部必填（CHECK `ck_rollout_segment_sealed_fields`）；`deleted` 与 `deleted_at` 严格同真同假 |
| 依据 | 用户决策（2026-09-10）。三段式是唯一能同时满足 §5.4「事务成功后才标 sealed」与 §1.7「用 manifest 定位热段」的取值 |
| 影响 | Phase 1/2 写路径须先把段登记为 `open`，封存后改 `sealed`；reconcile 需同时处理 open 段与 sealed 段 |

### ADD-002 新增 `BEFORE DELETE` 触发器（文档完全未提）

| 项 | 内容 |
|---|---|
| 文档位置 | 无。§5.2 C-1 仅要求"所有新增 FK/索引都采用可回滚、可重复执行的迁移写法" |
| 实现 | `conversation.clear_rollout_pointer_on_segment_delete()` + `trg_rollout_segment_clear_pointer`，在删除段前把 `conversation_messages` 的四个指针列一起置空 |
| 依据 | **实测发现的硬冲突**：PostgreSQL 17 的 `ON DELETE SET NULL (列清单)` 列清单必须是外键引用列的子集，因此 FK 动作只能清空 `segment_id`；而 `ck_conv_messages_rollout_pointer` 要求四列同真同假 → 删段直接违反 CHECK 而失败（已在 conversation_test 复现）。用户决策（2026-09-10）加触发器兜底 |
| 影响 | 删段语义由"FK 动作"与"触发器"共同保证；后续若要移除该 FK，触发器必须一并评估。已在迁移的 downgrade 中显式 DROP TRIGGER/FUNCTION |

### ADD-003 删除合规影响面比 §2.6 记载的多一处

| 项 | 内容 |
|---|---|
| 文档位置 | §2.6「实施影响面」只写 `account_purge` 活跃状态列表加 `pending_batch` |
| 实现 | 除 `backend/memory/services/account_purge.py` 外，**同时修改** `backend/memory/persistence/operations.py::request_cancel`，把 `pending_batch` 纳入可取消集合 |
| 依据 | 代码事实：`request_cancel` 的 `else: return None` 分支会让 `pending_batch` 直接不可取消，账号删除时待批量证据会逃过 §21.3 的合规清理。文档漏记了这一处 |
| 影响 | 无新增语义，属于把文档意图补全 |

### ADD-004 revision id 受 `varchar(32)` 限制

| 项 | 内容 |
|---|---|
| 文档位置 | §5.2 C（"实际 revision id 以执行 `alembic heads` 后生成，不能盲写旧 head"） |
| 实现 | conversation 链 revision 取 `0007_rollout_manifest`（20 字符），**不是**描述性的长名 |
| 依据 | 代码/数据库事实：`conversation_alembic_version.version_num` 是 `varchar(32)`；初次尝试的 `0007_conversation_rollout_manifest`（34 字符）在写入版本行时抛 `StringDataRightTruncation`。本链既有惯例即缩写（如 `0004_ks_alias_group_unique`） |
| 影响 | 后续链迁移命名须 ≤32 字符 |

### ADD-005 索引超出 §5.2 C 列举范围

| 项 | 内容 |
|---|---|
| 文档位置 | §5.2 C-1 列举 `thread_id+ordinal_start`、`object_key`、`turn_id`；C-2 列举 `(status, next_run_at, user_id)` |
| 实现 | 另加 `ix_conv_rollout_segments_status (status, created_at)` 与 `ix_memory_operations_batch_operation (batch_operation_id) WHERE NOT NULL` |
| 依据 | 前者的唯一约束已覆盖 thread/turn 两组，故只补 object_key；新增两索引分别服务 §5.4 的 reconcile 状态扫描与 §2.6 状态机第 3/4 步的"按 batch_operation_id 回写成员" |
| 影响 | 纯性能索引，无语义影响 |

### ADD-006 `ObjectStat` / `ObjectRef` 拆分

| 项 | 内容 |
|---|---|
| 文档位置 | §5.2 A 只写 `ObjectRef`（key、etag/hash、size、content type） |
| 实现 | 拆为 `ObjectStat`（key/etag/size/content_type）与 `ObjectRef(ObjectStat)`（额外 `sha256`）。`put_immutable` 返回 `ObjectRef`，`head`/`list_prefix` 返回 `ObjectStat` |
| 依据 | 只有 `put` 的调用方持有对象字节、能本地算内容哈希；`head`/`list` 只有服务端元数据。§5.5 也要求 Local adapter"返回独立 sha256 和 size"，而 §5.5 又要求同一 key 异内容报 hash 冲突——区分二者才能同时成立 |
| 影响 | Phase 3 三个 adapter 的返回类型按此实现；调用方不得假设 `head` 能给出 sha256 |

### ADD-007 rollout 记录类型与 payload 模型按"一次定全"实现

| 项 | 内容 |
|---|---|
| 文档位置 | §5.2 A 只点名 `ThreadMetaPayload` / `TurnStartedPayload` / `TurnCompletedPayload` 三个模型；§1.5 白名单表另有若干类型；`memory_prime` / `memory_tool_call` / `memory_tool_result` 出现在 §2.4（Phase 5） |
| 实现 | 12 种记录类型一次定全（`ROLLOUT_RECORD_TYPES`），并为**每一种**定义 payload 模型，含文档只给字段描述的 `turn_context_snapshot` / `evidence_set` / `embedded_queries` |
| 依据 | 用户决策（2026-09-10）：契约需要长期稳定并进快照测试，避免每个 Phase 回头改契约 |
| 影响 | Phase 5 新增工具链路时**不应**再改 `contracts/rollout.py`；若确需新增字段，属于破坏"一次定全"的决策，需在本文档追加条目 |

### ADD-008 若干默认值文档未给，由决策补齐

| 配置 | 取值 | 文档情况 |
|---|---|---|
| `conversation_rollout_segment_max_bytes` | `16_777_216`（16 MiB） | §5.2 B 只说"有界正整数…默认值须由现有 settings 风格给出并在压测后校准" |
| `conversation_rollout_retention_days` | `30` | §5.2 B 只说"沿用当前 retention"；实测现有 SSE 事件与 checkpoint retention 均为 30 天 |
| `memory_summary_max_users_per_run` | `50` | §2.6 只提"有界认领三件套"，未给值 |
| `memory_summary_llm_concurrency` | `8` | §2.6 引 codex `stage_one` 常量"并发 8"作参考，但未定为默认值 |
| `memory_summary_daily_time` 类型 | `datetime.time`（默认 `00:00`） | §2.6 给默认值未给类型；现有 scheduler 用 `daily_at: time`，故对齐 |

依据：均为用户决策（2026-09-10）。

### ADD-009 `summarize_user_memory_batch` 的 operation 路由文档未给

| 项 | 内容 |
|---|---|
| 文档位置 | §2.6 只说新增该 operation 类型，**未给** `input_kind` / `priority` |
| 实现 | `OPERATION_ROUTING["summarize_user_memory_batch"] = ("evidence", PRIORITY_P2)`，即 `max_attempts = 4`，claim 排序在 P0/P1 之后、P3/P4 之前 |
| 依据 | 用户决策（2026-09-10）：语义上是"对话总结"的批量形态，与 `conversation_evidence` 同级 |
| 影响 | Phase 6 生成批量 operation 时按此路由；若压测发现重试不足需显式改本条目 |

### DEV-003 文档 §5.8 引用的既有状态名与代码不符（文档笔误，仅登记）

| 项 | 内容 |
|---|---|
| 文档位置 | §5.8 开头："普通 `pending`、`running`、`succeeded`、`failed` 不改变" |
| 实现 | 代码中**不存在** `pending` 与 `failed`；实际枚举为 `queued` / `running` / `retry_wait` / `succeeded` / `needs_review` / `dead_letter` / `cancelled`（`backend/memory/contracts/common.py`） |
| 依据 | 代码事实。实现以代码为准，不改状态名 |
| 影响 | Phase 6 实现批量的 claim/lease/retry/dead-letter 时，须按实际枚举而非 §5.8 的名称 |

### ADD-010 超出 Phase 0 范围的既有问题修复

用户 2026-09-10 明确要求修复既有失败，以下条目与 memory-rebuild 无关，仅登记以免混淆：

| 文件 | 改动 | 原因 |
|---|---|---|
| `backend/community/storage/kodo.py` | 一行 `ruff format` 归一 | 基线即不满足 `ruff format --check`，会使 backend-lint 门禁永远为红 |
| `tests/integration/test_backup.py` | head 断言由硬编码 `0006_global_maintenance_gate` 改为 `backup_module._current_migration_head()` | 任何新增链迁移都会让硬编码断言失效；改为动态后与 `backup.py::_validate_migration_revision` 同源 |
| `tests/unit/test_backup_manifest.py` | 参数化补入 `0007_memory_batch_operations` | 保持"current head 与 ancestor 都接受"的原意 |
| `tests/study/test_tasks_sessions_api.py` | 硬编码日期改为相对本周一推导（`NEXT_TUESDAY` / `LATER_MONDAY`） | 硬编码 `2026-08-31` 随时间推移变成过去日期，被"日期越界"分支拦截而失败。**顺带修正了一处测试失真**：原"休息日"用例其实走的是越界分支（service 中越界检查在休息日检查之前），改用未来周二后才真正覆盖休息日分支 |
| `tests/integration/conftest.py`、`tests/failure_recovery/conftest.py` | 新增会话级 `_cleanup_test_graph_nodes` | 图谱注册表按设计跨测试保留，而测试会插入 `source_file='test.md'` 的假节点（`n9001`）；残留会让下一次 `sync-knowledge-graph --apply` 检测到漂移并以退出码 3 拒绝执行，导致 CI 第二次运行中断 |

### OPEN-001 `MessageRow` 未补 rollout 指针字段（刻意延后）

| 项 | 内容 |
|---|---|
| 现状 | 迁移已给 `conversation_messages` 增加 4 个可空指针列，但 `backend/conversation/contracts/domain.py::MessageRow` **未**同步增加对应字段 |
| 依据 | §5.2 A 说 domain.py 的扩展"只增加可选字段"（允许但不强制）；而 §5.13「逐文件改动清单」**没有列** `contracts/domain.py`。且 `MessageRow` 当前无任何使用点，`persistence/messages.py` 用的是 `SELECT *` + 裸 dict，不受影响 |
| 处置 | 等 Phase 2 接入 reader、真正需要类型化指针时再补。若届时需要，属正常加法扩展 |
| 风险 | 低。列表接口返回的是 dict，新增列只是多几个 key |

### OPEN-002 Phase 0 未做任何读路径接线（符合计划，登记以备核对）

Phase 0 只落契约/配置/迁移，**未**修改任何节点、worker 或 reader，因此"flag 未显式设置时行为不变"是构造性成立的（新增 19 项配置全部 default False，且无任何代码读取它们）。
Phase 1 起才会出现真实的 flag 分支，届时该结论需要重新验证。

---

## Phase 1：Conversation Rollout 本地链路

### DEV-004 §5.3 的"六节点接入"清单与 §1.5 白名单对不上

| 项 | 内容 |
|---|---|
| 文档位置 | §5.3 要求"在 `snapshot.py`、`memory.py`、`rewrite.py`、`evidence.py`、`answer.py`、`finalize.py` 的关键成功/降级路径调用 recorder"；但 §1.5 的记录白名单只有 9 类，**没有** memory 活动对应的类型 |
| 实现 | `memory.py` 与 `answer.py` **不产生独立记录**。memory 的产出（`memory_status`）内含在 `turn_context_snapshot`；answer 的产出（回答正文）由 `finalize` 统一落 `assistant_message` |
| 依据 | 用户决策（2026-09-10）：以 §1.5 白名单为准，不新增第 13 种记录类型 |
| 影响 | §5.3 的字面节点清单未完全满足；若将来确实需要独立的 memory 活动记录（例如记录检索耗时与命中数），需要新增记录类型并改契约快照 |

### DEV-005 "writer 按 thread_id 分桶持有句柄"未实现为桶式句柄

| 项 | 内容 |
|---|---|
| 文档位置 | §1.5「并发：同一 thread 的 turn 已被 DB lease 串行化；writer 按 thread_id 分桶持有句柄」 |
| 实现 | recorder 只维护**一个活动段**；并发 `open_turn` 直接返回 `None` 并告警，不覆盖、不排队 |
| 依据 | 代码事实：`graph_worker._poll_once` 注释明确"先单并发，简单可靠"，worker 串行执行 turn，同时最多一个段。且 §1.7 已否定"跨 turn 持有句柄"，桶式句柄与之矛盾 |
| 影响 | **若将来提高 worker 并发**，必须先把 recorder 改成按 thread_id 分桶（含 ordinal 分配的并发保护），否则第二个 turn 的段会被拒绝记录。用户决策（2026-09-10）已确认按单活动段实现 |

### ADD-011 Phase 1 本地段路径文档未给

| 项 | 内容 |
|---|---|
| 文档位置 | §1.5 只给 thread 级"逻辑文件名"，§5.5 只给对象存储 key，**都没有**本地热段的物理路径 |
| 实现 | `{root}/threads/YYYY/MM/DD/<thread_id>/<ordinal_start>-<segment_id>.jsonl`；日期取 thread 创建时间（UTC），`ordinal_start` 前导补零使字典序与数值序一致 |
| 依据 | 用户决策（2026-09-10）：对齐 §5.5 的对象 key 结构，Phase 2 上传时几乎不用改路径逻辑 |

### ADD-012 新段 `ordinal_start` 的确定方式文档未给

| 项 | 内容 |
|---|---|
| 文档位置 | §1.5 只说 ordinal"文件级单调递增"，§1.7 说用 manifest 定位热段；但 Phase 1 还没有 manifest |
| 实现 | 扫描该 thread 的段目录，取最后一个段的最后一个**完整**行的 ordinal + 1（`read_last_ordinal` 只读文件尾部 64KB）；末尾半行不参与计算 |
| 依据 | 用户决策（2026-09-10）：thread 级连续，Phase 1 靠扫描，Phase 2 接 manifest 后改走索引 |
| 影响 | Phase 2 引入 manifest 后，此扫描逻辑应作为"manifest 缺失时的回落"，不能直接删除 |

### ADD-013 `thread_meta` 由 recorder 自动写入，且"首次物化时才写"

| 项 | 内容 |
|---|---|
| 文档位置 | §5.3 写入顺序第 1 条写"`thread_meta`（第一次物化文件时写入）"，但没说由谁写 |
| 实现 | recorder 在**首次真正记录**时自动补 `thread_meta` 行（占用段内最小 ordinal），并拒绝调用方显式记录该类型 |
| 依据 | "空 turn 不创建空文件"是 §5.3 验收第一条。若由调用方在 `open_turn` 后立即记录 thread_meta，则每个 turn 都会物化文件（因为 thread_meta 总是第一条），该验收不可能满足 |
| 影响 | 每个段都自带 `thread_meta`（段自包含，便于独立重放）；代价是同一 thread 的多个段会重复首行元数据 |

### ADD-014 `thread_created_at` 的来源文档未给

| 项 | 内容 |
|---|---|
| 文档位置 | §1.5 说段目录按 thread 创建时间分片，但没说写路径如何拿到它 |
| 实现 | `graph_worker._claim_next_turn` 的 claim 查询改为 `conversation_turns JOIN conversation_threads`，附带 `thread_created_at` 列；`FOR UPDATE OF t` 限定只锁 turn 行（避免与 finalize 的 thread 行锁产生额外争用）。缺失时（单测直接构造 turn dict）退化为 `clock.now()` |
| 依据 | 代码事实：原 claim 查询是 `SELECT * FROM conversation_turns`，不含 thread 的创建时间 |
| 影响 | 退化路径只影响分片目录的选择，不影响 ordinal 单调性与记录内容 |

### ADD-015 `embedded_queries` 的接入点在 `retrieval.py`

| 项 | 内容 |
|---|---|
| 文档位置 | §1.5 白名单包含 `embedded_queries`，但 §5.3 的接入点清单**没有** `retrieval.py` |
| 实现 | 在 `retrieval.py::embed_subqueries` 落盘，只记 `model` + `dimensions`（向量本身不落） |
| 依据 | 代码事实：`embedded_queries` 只有该节点生产；不接在这里就没有任何地方能落这个白名单类型 |

### ADD-016 队列写满的等待上限文档未给

| 项 | 内容 |
|---|---|
| 文档位置 | §1.5 只说"有界 `asyncio.Queue(256)`"，未规定写满后等多久 |
| 实现 | `QUEUE_PUT_TIMEOUT_SECONDS = 5.0`：超时即丢弃该条并计 `rollout_dropped_total`，不无限期阻塞图执行 |
| 依据 | §1.5「写 IO 失败…降级不拖垮 turn」：磁盘挂起时不能把 turn 一起挂住 |

### ADD-017 `graph_version` 取值来源文档未给

| 项 | 内容 |
|---|---|
| 文档位置 | §1.2 首行元数据含 `graph 版本`，未规定取值 |
| 实现 | 模块常量 `CONVERSATION_GRAPH_VERSION = "conversation-graph-v1"` |
| 影响 | 图拓扑变更时需要人工升版；未与任何代码常量联动 |

### ADD-018 "每段最多一条 `turn_completed`"不变式文档未给

| 项 | 内容 |
|---|---|
| 文档位置 | 无 |
| 实现 | recorder 保证一个段内 `turn_completed` 只落一条：重复记录被拒 |
| 依据 | 正常路径由 finalize 写 completed，异常路径由 runner 补写 failed。若 finalize 成功之后图仍抛错（例如 checkpoint 收尾失败），没有该不变式就会写出"先 completed 再 failed"的矛盾终态 |

### ADD-019 段大小防爆阈值的处理方式

| 项 | 内容 |
|---|---|
| 文档位置 | §5.3「达到阈值时记录可观测的 `segment_size_guard_triggered`，不得在一个 turn 中静默丢行。若阈值处理需要拆段，必须显式增加 segment 边界记录并在 Phase 2 验收前补充重放测试」 |
| 实现 | Phase 1 **只告警并计数**，继续写入、不丢行、不拆段（每段触发一次告警，避免刷屏） |
| 依据 | 拆段需要新的段边界记录类型与重放测试，属 Phase 2 范围；"不静默丢行"是硬要求，继续写入即满足 |

### ADD-020 新增指标超出 §5.12 列举

| 项 | 内容 |
|---|---|
| 文档位置 | §5.12 列出 `rollout_records_total`、`rollout_write_failed_total`、`rollout_flush_latency`、`rollout_queue_depth`、`rollout_segment_bytes` |
| 实现 | 落到 `backend/conversation/metrics.py`：`rollout_records_total{record_type}`、`rollout_records_written_total`、`rollout_records_rejected_total{record_type}`、`rollout_write_retry_total`、`rollout_write_failed_total`、`rollout_dropped_total{reason}`、`rollout_segment_guard_triggered_total`、`rollout_queue_depth`、`rollout_flush_latency_seconds` |
| 差异 | 把"写入量"拆成 `records_total`（入队）与 `records_written_total`（落盘）两个计数，便于区分"入队成功但落盘失败"；新增 rejected/dropped/retry/guard 四个计数。`rollout_segment_bytes` 未做成指标（每段字节数基数随 turn 增长），改为在段达到阈值时告警 |
| 依据 | §5.3「如需统计，统一落到现有 metrics.py，不在节点内散落指标实现」 |

### ADD-021 rollout 记录在数据库事务**提交之后**落盘

| 项 | 内容 |
|---|---|
| 文档位置 | §5.3 只规定"finalize 后等待 flush ack"，未规定与 DB 事务的先后 |
| 实现 | finalize 在事务内读用户消息行，事务提交成功后才记录 `user_message` / `assistant_message` / `turn_completed` |
| 依据 | rollout 是"事实源"，不应记录未提交的事实；fencing 失败等早退路径不回滚 rollout。已加测试固定该行为 |

---


## Phase 2：Manifest、Reader、恢复与删除

### DEV-006 删除链路改为"先删对象、再落 tombstone"（与 §1.8 字面顺序相反）

| 项 | 内容 |
|---|---|
| 文档位置 | §1.8「超过 retention → 物理删除段对象 + manifest 行 + messages 索引行」；§5.5「retention 执行必须先写删除审计/状态，再删除对象」 |
| 实现 | `thread_deletion`、CLI 的 `delete-thread-rollouts` / `retention-scan` **一律先物理删除对象、全部成功后才落 tombstone**；任一对象删除失败则整段不标 tombstone（保持原状态可重跑），删除命令返回非 0 |
| 依据 | 若按字面顺序，对象删除失败会留下"manifest 已是 deleted、对象仍在"的残留。而 reconcile 的孤儿判定只把**非 deleted** 行的 object_key 当作有效引用，这种残留既不会被报成孤儿、retention 又只扫 `sealed`，于是**永久泄漏且无人可见**（子代理在实现 reconcile 时发现的缺口 A） |
| 影响 | 若 tombstone 事务回滚，会留下"manifest 指向已删对象"——由 reconcile 的 `sealed_object_missing` 检出并收敛。这是刻意选择的 fail-safe 方向：删除合规的第一要务是数据**物理**消失 |

### DEV-007 Phase 2 的对象存储只实现 Local + Fake

| 项 | 内容 |
|---|---|
| 文档位置 | §5.5 要求实现 `LocalRolloutObjectStore` 与 `QiniuKodoRolloutObjectStore` |
| 实现 | Phase 2 只做 `LocalRolloutObjectStore`（目录模拟 bucket）与 `FakeRolloutObjectStore`（内存，测试故障注入）；**没有** Kodo 适配器 |
| 依据 | 用户决策（2026-09-10）：Kodo 属 Phase 3，Phase 2 先用 Phase 0 定好的 `RolloutObjectStore` 协议跑通全链路，不阻塞 |
| 影响 | 配置为 `CONVERSATION_ROLLOUT_OBJECT_STORE=kodo` 时 CLI 显式失败（退出 2），不静默切回本地（§5.5 要求）。Phase 3 只需新增一个 adapter，recorder/sealer/reader/reconcile 都不改 |

### ADD-022 `open` manifest 行懒创建

| 项 | 内容 |
|---|---|
| 文档位置 | §1.7 步骤 2 要求"以 thread_id + manifest 定位本地热段"，但没说 `open` 行何时创建 |
| 实现 | 与本地段文件**同一时机**创建：首次真正写入（文件物化）时才 `insert_open` |
| 依据 | 用户决策（2026-09-10）。与 Phase 1「空 turn 不建文件」语义一致——否则空 turn 会在 manifest 留下无对象的 open 行，reconcile 还得额外清理 |

### ADD-023 崩溃恢复策略与迁移 0008

| 项 | 内容 |
|---|---|
| 文档位置 | §1.7 步骤 2 提到"未命中且存在未封存段 → 从对象存储拉回续写"，但未规定"本地文件不在"时怎么办；§5.2 C 的 `uq_rollout_segment_turn UNIQUE (turn_id)` 也不允许一个 turn 有两个段 |
| 实现 | 重新 claim 同一 turn 时查 `open` 行：本地文件仍在 → 复用 `segment_id`/`ordinal_start` 追加续写（并抑制重复写 `thread_meta`）；本地文件不在（换节点）→ 把旧 `open` 行标 `deleted` 并新建段。为此新增迁移 `0008_rollout_segment_turn_uq`，把 turn 唯一约束改为**部分唯一索引**（`WHERE status <> 'deleted'`） |
| 依据 | 用户决策（2026-09-10）：同节点续写、跨节点标废重建 |
| 影响 | Phase 2 仍未实现"从对象存储拉回未封存段"（未封存段不上传，拉不回来）；跨节点会丢失该 turn 崩溃前已写入的段内容，但新段会重写本 turn 的完整记录（消息正文以 DB/后续记录为准） |

### ADD-024 本地热缓存路径由对象 key 反查

| 项 | 内容 |
|---|---|
| 文档位置 | 无。文档只给了本地路径规则与对象 key 规则，没说封存后如何由 manifest 找回本地文件 |
| 实现 | `local_path_for_object_key()`：本地段路径与对象 key **同构**（只差 `rollouts/` 前缀与根目录），因此直接映射 |
| 依据 | manifest 只记**段**创建时间，而日期目录取自 **thread** 创建时间（§1.5），跨零点时两者不同日，无法由 manifest 反推目录。用 key 反查既精确又 O(1) |
| 备注 | 这解释了子代理报告的"open 段本地路径无法从 manifest 算出"：`open` 行还没有 object_key，确实只能用 `find_segment_dir` 反查目录（reconcile 即如此），已封存段则不受影响 |

### ADD-025 Reader 必须显式检查段 `status`

| 项 | 内容 |
|---|---|
| 文档位置 | §5.4 验收要求"删除后的 message 永不从 rollout 回读"，但没说实现方式 |
| 实现 | `RolloutReader` 在 `read_message_content` / `read_segment` / `read_thread_records` 三处都先判 `status <> 'deleted'`，否则返回 None 走回退 |
| 依据 | 迁移 0007 的清指针触发器只在**硬删除** manifest 行时触发，而 thread 删除与 retention 走 `mark_deleted` **软删**——软删不会清 `conversation_messages` 的四个指针列。若不显式判 status，"删除后不可回读"就只能寄希望于对象恰好已被物理删除，存在"已 tombstone 但对象未删"的窗口会泄漏已删数据（子代理发现的缺口 B，已由新增测试固定） |

### ADD-026 指针读取做区间与 ordinal **双重**校验

| 项 | 内容 |
|---|---|
| 文档位置 | §5.4 只说"按 `(segment_id, ordinal, byte range)` 读取并校验 hash" |
| 实现 | 取出的字节区间必须①落在文件内、②以换行结尾、③恰好一条记录、④记录 ordinal 等于指针 ordinal |
| 依据 | 只校验区间时，"指针被写错但仍落在文件内"会静默返回**别的消息的正文**——比读不到危险得多 |

### ADD-027 封存加 turn fencing

| 项 | 内容 |
|---|---|
| 文档位置 | §5.4 说"所有更新带 thread/turn fencing 校验"，未给具体形式 |
| 实现 | `seal()` 在 UPDATE 里加 `EXISTS (SELECT 1 FROM conversation_turns WHERE turn_id = ... AND lease_owner = :owner AND lease_generation = :gen)`；runner 从 turn 行取出 `(worker_id, lease_generation)` 传入 |
| 依据 | 与 finalize 的 fencing 同源。失租 worker 不得写 manifest——否则被回收的 turn 会与新执行者产生两条逻辑冲突的钟 |
| 影响 | fencing 不通过时对象已上传（顺序铁律决定），成为孤儿对象，由 reconcile 检出 |

### ADD-028 reconcile 的"有效引用集合"排除 tombstone

| 项 | 内容 |
|---|---|
| 文档位置 | §5.4 只说扫描"已上传未登记"等状态 |
| 实现 | 孤儿对象判定时，manifest 侧的有效引用只取 `status <> 'deleted'` 的行 |
| 依据 | tombstone 的语义是"这个对象本应已被物理删除"，把它算作有效引用会让 DEV-006 描述的那类残留永远不可见 |

### ADD-029 运维 CLI 的五个子命令

| 项 | 内容 |
|---|---|
| 文档位置 | §5.4 只说"增加 `backend/conversation/cli/rollout.py` 或等价运维入口：reconcile、verify、export、delete/retry"；§5.5 列了 5 个具体命令名 |
| 实现 | `verify-manifest`（只读，非空退出 1）、`reconcile-orphans`、`export-thread`、`delete-thread-rollouts`、`retention-scan`，全部支持 `--dry-run`，破坏性命令必须显式 `--apply` |
| 测试状况 | **只有手工端到端冒烟，没有仓库内集成测试**；假会话工厂不校验 SQL 语义。已登记为 OPEN-005 |

### OPEN-003 tombstone 之后对象残留的可见性（已收敛）

原缺口：tombstone 先落、对象后删，若对象删除失败，残留对象永不被报为孤儿（孤儿只查"无任何 manifest 引用"）。
处置：**DEV-006 改顺序 + ADD-028 改引用集合**，两处一起把该残留变成可观测的 `orphan_object`。已关闭。

### OPEN-004 软删不清 `conversation_messages` 指针（根本修法待定）

现状：迁移 0007 的清指针触发器只在硬删除 manifest 行时触发；`mark_deleted` 软删后，消息行的四个指针列仍指向已删除段。
兜底：reader 显式判 `status`（ADD-025），因此不会回读已删数据。
待决：是否在 `mark_deleted` / `mark_thread_deleted` 里一并清空指针列。**不清的理由**是保留"这条消息曾属于哪个段"的审计线索；**要清的理由**是避免悬垂指针在降级路径上被误用。Phase 3 决定 retention 物理清理策略时一并定。

### OPEN-005 reconcile / CLI 缺少仓库内集成测试

现状：`backend/conversation/rollout/reconcile.py` 与 `backend/conversation/cli/rollout.py` 只有单元测试（reconcile 6 例，用假会话工厂）+ 手工冒烟，**没有**真实 PostgreSQL 的集成测试。假会话工厂按 SQL 文本分发，JOIN/WHERE 写错它发现不了。
待决：Phase 3 补 `tests/integration/test_rollout_reconcile.py`，覆盖五类 finding 的真实 SQL 路径与 CLI 五个子命令。

---

---

## Phase 3：七牛云 Kodo 对象存储与生命周期

### DEV-008 thread 删除是**物理删除**，而 §1.8 说「移入 archived_threads/ 归档」

| 项 | 内容 |
|---|---|
| 文档位置 | §1.8「thread 删除（thread_deletion.py 链路挂接）→ 段对象移入 `archived_threads/` 前缀（对应 codex `archived_sessions/`）」；但 §5.4 又说"删除 thread 时先阻止新读写，再**删/标记** manifest、对象和索引" |
| 实现 | Phase 2 起 `thread_deletion` 与 CLI 一律**物理删除**段对象 + 落 tombstone，**没有**实现归档前缀 |
| 依据 | §1.8 与 §5.4 相互矛盾，取 §5.4 的删除语义。更重要的理由是合规：用户删除会话却把正文归档保留，等于删除没生效；删除合规的第一要务是数据物理消失（同 DEV-006 的取舍） |
| 影响 | `archived_threads/` 前缀当前**完全未使用**。若确实需要"用户可见的归档（不删除）"能力，那是一个**独立功能**（应先有产品入口），不是删除链路的替代路径。已登记为 OPEN-006 |

### ADD-030 Kodo 适配器把 SDK 收敛到可注入的 client 门面

| 项 | 内容 |
|---|---|
| 文档位置 | §5.5 只要求"所有 SDK 类型只出现在 adapter 内" |
| 实现 | 再套一层 `_KodoClient` 协议（put/fetch/stat/list_prefix/delete/private_url 六个方法），默认实现 `_QiniuKodoClient` 是唯一 `import qiniu` 的地方；测试注入假门面 |
| 依据 | 用户"暂时没有七牛账号"。没有这层门面，适配器的错误分类、幂等、range、612 语义全都无法在无账号时验证。有了它，26 个单测覆盖全部分支且不触网 |

### ADD-031 对象存储按配置构造，收敛到 `rollout/factory.py`

| 项 | 内容 |
|---|---|
| 文档位置 | §5.5 只说业务层只依赖协议与 ObjectRef |
| 实现 | `build_rollout_object_store(settings)` 一处决定 Local/Kodo；worker、app、CLI 三个装配点共用 |
| 依据 | 三个装配点各写一遍 if-else 必然漏校验。工厂在 kodo 缺配置时**直接抛错**，不静默降级（§5.12） |

### ADD-032 Kodo `health_check` 不写探测对象

| 项 | 内容 |
|---|---|
| 文档位置 | §5.5 只说"健康检查" |
| 实现 | 只 `list_prefix("rollouts/", limit=1)`，不在真实 bucket 里创建任何对象 |
| 依据 | 健康检查会周期性运行；每次写一个探测对象既产生垃圾又可能触发生命周期规则误判。Local 实现写的是本地临时文件，无此顾虑 |

### ADD-033 Kodo 上无法区分"同 key 同内容"与"同 key 异内容"

| 项 | 内容 |
|---|---|
| 文档位置 | §5.5「同一 object key 重复 put 是幂等的；不同内容使用同一 key 时拒绝覆盖或报告 hash 冲突」 |
| 实现 | 用服务端 `insertOnly` 策略：对象已存在即返回 614，适配器**一律**报 `ObjectHashMismatchError` |
| 依据 | Kodo 的 ETag 是服务端语义（可能是分片 MD5），**不等于**内容 sha256，因此无法在不上传的前提下判断既有对象是否与本次字节相同。Local/Fake 能读到内容，所以它们可以真正幂等 |
| 影响 | **同一段重放 put 在 Kodo 上会报冲突而不是幂等成功**。封存流程本身用 `(thread_id, ordinal_start)` 幂等键 + frozen manifest 避免了重复封存，因此正常路径不会触发；但若有人手工重放封存，需要先删对象。这条差异必须让运维知道 |

### ADD-034 真实网络 smoke 的边界与跳过策略

| 项 | 内容 |
|---|---|
| 文档位置 | §5.5「获得账号后增加一组受控的真实环境 smoke，不把真实环境测试作为默认 CI」 |
| 实现 | `tests/conversation/test_rollout_kodo_smoke.py`：四项 Kodo 配置齐备才运行，否则 3 个用例全部 skip；只在 `rollouts/_smoke/<uuid>/` 前缀下写入，finally 里清理自己写的 key；不请求任何 DB fixture，可脱离 PostgreSQL 单跑 |
| 依据 | 用户决策（2026-09-13）：允许受控读写、测完自删 |
| 现状 | **从未真实运行过**（当前工作区无七牛凭据，见下方 OPEN-006 旁的说明）。拿到账号后一条命令即可验证 |

### OPEN-006 `archived_threads/` 归档前缀完全未实现

现状：DEV-008 决定 thread 删除走物理删除，因此 `archived_threads/` 前缀没有任何代码使用，Kodo 侧的归档生命周期规则也无从配置。
待决：是否存在"归档而不删除"的产品需求。若有，应先定义用户入口与语义（归档后是否仍可被 reader 读到？是否计入 retention？），再实现 key 前缀迁移与状态机。**Phase 3 不擅自实现**。

---

### 关于七牛凭据的说明（2026-09-13）

用户提出"`.env` 里面有七牛账号"，但实际排查结果：主工作区与 worktree 的 `.env` 均只含 AI 服务凭据（MinerU / DeepSeek / DashScope / Embedding / Rerank），**没有任何 Kodo/Qiniu 键**；`deploy/env.production.example` 里的 `KODO_*` 全是占位符；shell 环境变量中也没有。
因此 Kodo 适配器只完成了"无账号可验证"的部分（门面注入 + 26 个单元测试 + 配置校验 + 装配接线），真实网络 smoke 处于**已实现未执行**状态。

---

## Phase 4：长期记忆 Markdown schema v2

### DEV-009 §3.3 描述的"KG registry 强校验"在代码里**不存在**（文档有误，未按文档改）

| 项 | 内容 |
|---|---|
| 文档位置 | §3.3「拆除 [local_markdown.py](backend/memory/storage/local_markdown.py) 33–40 行 `validate_existing_topic_key` 对 KG registry 的强校验」 |
| 实际 | `validate_existing_topic_key`（`contracts/common.py`）是**纯语法校验**：长度、控制字符、路径穿越字符、连字符规则——docstring 写明用途是"API 路径参数防御"，**完全不触碰 KG registry**。写入路径（`_validate_memory_id`）也只做语法校验 |
| 结论 | **"mastery 主题自由建档"当前已经成立**，任何语法合法的 topic_key 都能建档，无需改代码 |
| 处置 | **没有按文档删除它**。删掉只会让存储层丢掉路径穿越防护（它同时被 `api/memories.py` 用于路由参数防御），而不会带来文档想要的效果 |

### DEV-010 §5.6 要求 Phase 4 升版三个提示词，实际只出两个

| 项 | 内容 |
|---|---|
| 文档位置 | §5.6「提示词绑定」列出 `build_mutation_plan_v1`→v2、`extract_candidates`→v3、**新增 `summary_consolidate_v1`** |
| 实现 | 只做了前两个。`summary_consolidate_v1` 是 consolidation 末段（Phase 7 / §5.9）的提示词，Phase 4 没有消费方 |
| 依据 | 用户决策（2026-09-13）：按批次边界划分，consolidation 提示词随 Phase 7 一起落地 |

### ADD-035 index v2 复用 `title`/`summary` 而非新开列

| 项 | 内容 |
|---|---|
| 文档位置 | §3.4 说索引项是 `memory_id | name | description | aliases | keywords | version | updated_at` |
| 实现 | 迁移只新增 `aliases` 与 `related_topic_keys` 两列；`name` 就是既有 `title` 列、`description` 就是既有 `summary` 列 |
| 依据 | 用户决策（2026-09-13）：它们本就是同一份投影，再开两列只会让两边漂移 |

### ADD-036 解析器**始终**双读；flag 只管写入与迁移

| 项 | 内容 |
|---|---|
| 文档位置 | §5.2 B 定义 `memory_schema_v2_read_enabled`（"启用 v1/v2 双读路径"）、§5.10 把它列为灰度阶段 |
| 实现 | `parse_learner` / `parse_mastery` / `parse_index` **无条件**按 frontmatter 的 `schema_version` 分派；不存在"关闭双读"的代码路径。v2 的写入由迁移任务与 `frontmatter_patch` 触发 |
| 依据 | 用户决策（2026-09-13）。理由：历史 `versions/` 永不迁移，双读是**长期能力**而非灰度开关；若让 flag 控制读取，一旦误关 flag，已迁移的文档立刻读不出来 |
| 影响 | `memory_schema_v2_read_enabled` 目前**没有代码读取方**（保留配置位，不产生行为） |

### ADD-037 `frontmatter_patch` 必须整套扩展契约，否则整批被拒

| 项 | 内容 |
|---|---|
| 文档位置 | §3.6① 说这是"配套代码变更"，但没说涉及几处 |
| 实现 | **三处** action Literal 必须同时改：`MutationPlanDraft.action`（planner 输入）、`CommitMutationPlan.action`（内部计划）、`MutationResult.action`（结果回执）。第三处是 mypy strict 逼出来的——漏掉它编译期就报错 |
| 依据 | 三者都是 `extra="forbid"` + strict JSON schema（`additionalProperties=False`）。模型一旦输出未声明的动作名，**整批计划**会被 `OpenAISchemaInvalidError` 拒绝，而不是只丢一条 |
| 影响 | 提示词里曾临时加过 3 行"若 schema 未扩展则改用 merge"的兼容说明；契约落地后**已删除**——留着会让提示词自我削弱 |

### ADD-038 迁移是**机械投影**，不调用 LLM

| 项 | 内容 |
|---|---|
| 文档位置 | §2.7 决议 A 组："迁移 = 后台维护任务把 current/ 按 v2 重新渲染并走正常 write_immutable_version 追加新版本" |
| 实现 | `_upgrade_to_schema_v2`：`name` 取既有标题、`description` 取概述/偏好的**首行**、`aliases` **留空**、`links` 从正文 `[[...]]` 现算 |
| 依据 | 机械投影可确定性重放、无 LLM 成本、失败可定位。迁移只保证"能读、格式合规"；更准确的 description/aliases 由 planner 后续用 `frontmatter_patch` 补 |
| 影响 | 迁移后所有文档的 `aliases` 为空数组，需要靠后续总结逐步充实 |

### ADD-039 frontmatter 补丁只在 name+description 齐备时才升 v2

| 项 | 内容 |
|---|---|
| 文档位置 | 无 |
| 实现 | `apply_frontmatter_patch` 应用补丁后，**只有 `name` 与 `description` 都非空**才把 `schema_version` 提到 v2 |
| 依据 | v2 解析器要求这两个字段必填。若补丁只给了一半就升版，会渲染出一个**自己下次读不出来**的文档——这是比"晚一版升级"严重得多的故障 |

### ADD-040 迁移任务的门控与调度时刻

| 项 | 内容 |
|---|---|
| 文档位置 | §5.6 只说"在 scheduler.py 增加 `migrate_markdown_schema_v2`"，未给时刻与门控实现 |
| 实现 | `ScheduledTask("migrate_markdown_schema_v2", daily_at=time(4, 15))`（避开既有 02:30–05:00 的整点任务）；门控 `SchedulerConfig.schema_v2_migration_enabled`，由 `settings.memory_schema_v2_migration_enabled` 装配，**默认 false** |
| 依据 | §5.6 要求复用 maintenance_runs + cursor 续跑，与 `verify_checksums` 同构（一次 run + 全局文档 cursor） |

### ADD-041 index 文档不在迁移任务处理范围内

| 项 | 内容 |
|---|---|
| 文档位置 | §5.6 步骤 3 说迁移顺序是 "learner → mastery → index" |
| 实现 | 迁移任务显式 `continue` 跳过 `memory_type == "index"`；index 由既有 `rebuild_index` 维护链路再生 |
| 依据 | index 是**文档的可再生派生物**（§3.4："文档是唯一事实源，index 是可再生派生物"）。单独迁移它会产生"index 说 v2、被索引的文档还是 v1"的不一致窗口 |

### ADD-042 提示词删掉了 `<memory_listing>` 标签引用

| 项 | 内容 |
|---|---|
| 文档位置 | §3.1 引用 OPUS-5 的 `<memory_listing>` 目录机制 |
| 实现 | v2 提示词改写为"会投影进长期记忆注册表目录"，不再写该标签 |
| 依据 | 全仓库 grep 后确认：只有 `memory-rebuild.md` 与 `OPUS-5.md` 提到该标签，`backend/` **无任何实现**。写了会让模型误以为存在该注入格式（D1 首轮注入属 Phase 5） |

### OPEN-007 `migrate_markdown_schema_v2` 的 DB 集成测试 —— **已关闭**

补齐：`tests/integration/test_memory_schema_migration.py`（5 例，真实 PostgreSQL + 真实
文件存储），覆盖 §5.6「迁移验收」中必须落盘才能证明的部分：

- v1→v2 后活动指针推进、current 可解析且 `[[link]]` 已现算；
- **历史 `versions/` 文件字节级不变**（迁移只走 `write_immutable_version` 追加新版本）；
- 二次迁移：`migrated=0`、版本号与 checksum 均不抖动；
- dry-run 不推进版本、不改 current；
- 坏文档进 `failures` 且**不阻塞**同批其他用户；
- `batch_size=1` 时返回 `continue` + `next_cursor`，续跑真的处理了后续行。

### ADD-044 集成测试抓出的两个**真实缺口**（单测与编译期都发现不了）

补 OPEN-007 的集成测试一次跑出两个缺口，二者都是"Python 常量改了、配套的注册点没改"：

| 缺口 | 现象 | 修法 |
|---|---|---|
| DB CHECK 约束未同步 | `contracts/common.py::OperationType` 加了 `migrate_markdown_schema_v2`，但 DB 侧的 `ck_memory_operations_operation_type`（0007 用显式清单建立）没加 → Scheduler 建 operation 时被 `CheckViolation` 拒绝 | 新增迁移 `0009_memory_migrate_schema_op` 扩展约束（按"不得改历史迁移"的规矩，不回头改 0007） |
| `route_by_type` 未登记 | `manager.py` 的 operation_type → 图分支映射没有该类型 → `InvalidPayloadError: 未路由的 operation_type` | 登记为 `maintenance` |

**教训**：新增一个 operation_type 至少要同步 **4 处**——`OperationType` Literal、
`OPERATION_ROUTING`、DB CHECK 约束、`manager.route_by_type`。前两处有 mypy 兜底，
后两处**只有真实库集成测试能发现**。这正是 §5.11 把集成测试列为独立层级的原因。

### ADD-043 `[[link]]` 的 name 全局唯一目前只是**生成侧纪律**

`markdown_schema.py` 实现了 `[[...]]` 的提取与 aliases 规范化，但**没有** name 唯一性校验。
提示词里把"name 全局唯一"写成生成侧要求。若将来发现模型产出重名 name，需要在
consolidation（Phase 7）里按 aliases 归并，而不是在解析器里报错（悬空与重名都属可接受
的中间态，最终由 nightly 批收敛）。

---

### 关于 Phase 4 的实现顺序（2026-09-13）

Phase 4 分两批落地：`001c42c`（schema 双读 + 提示词，中间提交）与收尾提交（契约扩展 +
index 投影 + 迁移任务 + 测试）。中间有一次 `memory_service.py` 被改坏并回滚：用
`str.replace("async def ", helper + "async def ", 1)` 插入辅助函数时命中了类内部的第一个
`async def`，导致整个文件缩进崩坏。教训：批量插入辅助函数必须用**唯一锚点**，改完立即核对目标位置——`replace(..., 1)`
命中类内部第一个 `async def` 时不会有任何报错，只会在后续测试里炸掉整个文件。

---

## Phase 5：Memory Prime 与 `memory.search` / `memory.read`

### ADD-045 memory 侧三个工具端点的落点与形状

| 项 | 内容 |
|---|---|
| 文档位置 | §5.7「工具契约」只给了语义要求（有界/可审计/用户隔离/默认不返回正文），没给路径与字段名 |
| 实现 | `POST /api/v1/internal/memory/tool/{search,read,prime}`，请求/响应模型落在 `backend/memory/contracts/results.py`，服务在 `backend/memory/services/memory_tools.py` |
| 路径依据 | 仓库既有内部路径规范是**单数** `memory`（`/api/v1/internal/memory/...`）；复数写法会与既有约定不一致 |
| 认证 | `require(actors=_READ_AGENT_ACTORS, scope=SCOPE_MEMORY_READ)`，`user_id` 只来自认证上下文；三个请求体 `extra="forbid"`，夹带 `user_id` 直接 422 `REQUEST_EXTRA_FIELD` |
| 验收 | `tests/unit/test_memory_tools.py`（35 例）+ `tests/integration/test_memory_tools_sql.py`（9 例，真实 PostgreSQL + 真实文件存储） |

### ADD-046 复数路径作为隐藏别名保留（用户裁决）

对话域客户端一度按复数列写；裁决为"**单数为规范，复数作别名**"。因此
`/api/v1/internal/memories/tool/*` 以 `include_in_schema=False` 保留，两套都能调，
但 OpenAPI 快照只含单数路径（不污染公开契约）。

### DEV-011 `memory.search` 的 keywords 档目前是**死代码**（keywords 列无生产者）

| 项 | 内容 |
|---|---|
| 文档位置 | §2.4 D3① 把 `keywords` 与 name/description/aliases 并列为匹配域，并给了"name/aliases > keywords > description"的排序权重 |
| 现状 | `memory_index_entries.keywords` **恒为空数组**：`memory_service._build_new_content`（learner/mastery）与 restore 三处都是硬编码 `"keywords": []` |
| 根因 | 全仓库没有任何写入 keywords 的通道——`FrontMatterPatch` 只有 name/description/aliases，两个提示词也不产出该字段；而 index.md v2 注册表里的 keywords **不回投影**到 PG（`rebuild_index` 只重写 index.md） |
| 影响 | 排序的中间档永远不命中；"判别性检索词"名存实亡（name/aliases 与 description 两档仍可用） |
| 处置 | 登记为 **OPEN-008**，等待决策：在 Phase 7 consolidation 里补生产者，或明确接受 keywords 档长期空转 |

### DEV-012 `description` 的真实语义比 §2.3 粗

§2.3 描述注册表 `description` 是"一句话 scope"。实际投影的是既有字段的截断：
mastery → overview 或 understood 前 3 条；learner → goals/preferences 前 3 条。
行为可用（选择性地反映了用户画像），但**不是**一句话摘要。未按文档改写投影规则
（那会改动既有记忆内容语义，属 Phase 7 consolidation 的范围）。

### DEV-013 prime 的 `generated_at` 只能取文件 mtime

§2.3 规定 `memory_summary.md` **没有 front matter**（首行恰好是 `v1`），因此没有权威
生成时间。当前取文件 mtime，无文件时为 `null`。若 Phase 7 要真时间戳，需要版本化
summary 或在 PG 侧存元数据。

### ADD-047 prime 的 `index_entries` 不做条数上限

冻结契约里没有 `index_entries_truncated` 字段，服务端按"**不静默丢条目**"全量返回，
条数上限交由 conversation 侧的 token 预算裁剪负责（§5.7「tool result 进入 graph state
前做 token/字符上限裁剪」）。用户主题很多时首轮注入会偏大，属已知代价。

### ADD-048 三个工具端点**不按 flag 做路由级门控**

`memory_prime_enabled` / `memory_tools_enabled` 不参与服务端挂载：端点始终存在，由
conversation 侧的 flag 决定是否调用（§5.2-B 的 flag 表只声明"是否启用该链路"，未要求
服务端隐藏路由）。若要"flag 关闭即不暴露路由"，需改 `backend/app.py` 条件挂载，属
独立决策，本 Phase 未做。

### ADD-049 `memory.read` 不限制 memory_id 的类型

§5.7 只要求"只允许读取已授权用户的文档"，因此 read 可读该用户**任意**活动文档
（learner/mastery/index 皆可）。若产品上要禁止读 index.md，需要显式收紧（1 行）。

### ADD-050 search 的 query 规范化只做空白处理

去首尾空白 + 去重，**不做** NFKC 折叠/分词：匹配是与 index 列原文逐字比较的子串关系，
额外折叠会让"看起来一样"的查询匹配不上原文。全空白 query → 空结果且不打库。

### DEV-014 `DegradedFlag` 是**封闭 Literal**，新降级标记必须同步扩展

| 项 | 内容 |
|---|---|
| 文档位置 | §5.7 要求 prime 降级记 `memory_prime_degraded`，并新增工具链路的降级语义；§17.4.1 把 `turn.degraded` 的 flags 定义为固定集合 |
| 现状 | `contracts/api.py::DegradedFlag` 是 `Literal[...]`，`AnswerCompletedPayload`/`TurnDegradedPayload` 都是 `extra="forbid"` |
| 后果 | Phase 5 WIP 期间 `_emit_degraded` 写 `memory_prime_degraded` 会被契约校验直接拒绝——**编译期不报错、单测不覆盖，只有写事件时才炸** |
| 处置 | 扩展 `DegradedFlag`：新增 `memory_prime_degraded` / `memory_prime_pin_missing` / `memory_prime_unavailable` / `memory_tool_degraded` / `memory_tool_truncated` / `memory_tool_budget_exceeded`，并在单测里用 `validate_event_payload` 实测过 |

### DEV-015 §5.7 的"非首轮沿用 **checkpoint** 中的 snapshot"在当前图结构下不可实现

§5.7 原文要求 prime "非首轮只沿用 checkpoint 中已确定的 snapshot，不重复 prime"。但
graph thread 是 `conv-turn:{turn_id}`（**每轮一个 thread**），checkpoint 天然不跨 turn；
把 prime 放进 Graph State 就等于每轮重新 prime。因此按 §2.4 D2（同一份文档里更具体的
决策）实现为：**首轮把 prime 快照 pin 到 rollout 的 `memory_prime` 记录，后续轮从 rollout
读回**；读不回时重新构建并记 `memory_prime_pin_missing`（可观测降级，不静默改变语义）。

**已知限制**：rollout 的 `memory_prime` payload 按 §1.5「大对象放引用」只存
`summary_hash` + `schema_version` + `generated_at` + `truncated` + `index_entry_count`，
**不含 summary 正文**。因此 `_load_pinned_prime` 目前只能还原"pin 的指纹"（`summary=""`）。
完整还原需要 memory 侧支持"按 hash 取指定版本 summary"——契约未定义，登记为待补。

### DEV-016 记忆工具服务落在 `services/memory_tools.py` 而非 `services/context_service.py`

§5.13 的 Memory 清单写的是"`backend/memory/services/context_service.py`：prime/tool/双源
协调"。实际新建了 `backend/memory/services/memory_tools.py`：既有 `context_service` 的
职责是"装配给对话的 LearningContext"（投影 + 双源协调），而三个工具是**只读、按请求
形态返回、不做投影**的另一类接口。把两者混在一个类里会让既有投影路径的测试面被动扩大。
未改 `context_service.py` 一行。

### ADD-053 memory citations 只登记 `memory.read` 的结果

`search` 只返回注册表条目（无正文、无 checksum），prime 的 summary 没有版本化载体——
两者都不满足 §5.7 "回查到具体 document version/checksum/section" 的要求。因此
`memory_citations` 只收 read 结果；search 的命中仍完整留在 rollout 的
`memory_tool_call` / `memory_tool_result` 记录里可审计（`document_versions` 字段）。

### ADD-054 assistant 消息行没有结构化列，记忆引用只进 turn event

§5.7 说把 memory citations 写进"assistant message/turn event 的结构化字段（若当前 API
尚无字段则只新增可选字段）"。`conversation_messages` 表**只有 content/content_hash**，
没有任何 JSON/元数据列；Phase 5 的交付清单里也没有 conversation 迁移。因此：

- `answer.completed` 事件新增**可选**字段 `memory_citations`（list[MemoryCitation]，默认空）；
- assistant 消息行不动（加列需要新迁移，属独立决策）；
- rollout 侧靠 `memory_tool_result.document_versions` 留痕。

### ADD-055 工具错误分类：401/403 让 Turn 失败，其余交回模型

§5.7 没写工具失败怎么分类。沿用 §16.2 的既有语义并细化为：

| 情形 | 处理 |
|---|---|
| 401/403 | 抛错让 Turn 失败（认证/权限问题不是"模型可以自己纠正"的错误） |
| 404 / `MEMORY_NOT_FOUND` | 作为工具结果交回模型（引用不存在的 memory_id 是正常纠错场景） |
| 其它 4xx | 作为工具结果交回模型，错误码 `MEMORY_TOOL_INVALID_ARGUMENT` |
| 5xx/超时/网络 | 作为工具结果交回模型 + 记 `memory_tool_degraded`，**不阻塞回答** |
| 网关判定 `executable=false`（arguments 不是合法 JSON 对象） | 不打网关，直接回 `MEMORY_TOOL_INVALID_ARGUMENT` |

### ADD-056 工具循环的两个独立上限 + 缓存命中不消耗预算

- **调用数上限 6**：直接复用 Phase 0 已定型的契约常量
  `contracts/rollout.py::MEMORY_TOOL_CALL_BUDGET`（=6），不新开配置项——§5.2-B 的配置表
  里没有这一项，而 §5.7 写死了 6。
- **轮数上限 6**：幂等缓存命中**不消耗**调用预算，因此"调用数"不再单调递增；若只靠调用数
  收口，模型反复请求同一个已缓存调用就会死循环。轮数是兜底上限。
- 缓存键 = 工具名 + **规范化后**参数的稳定 JSON（去空白、去重、边界裁剪、`sort_keys`）。
- 超限调用不执行、不伪造 `call_index`（契约 `call_index ∈ [1,6]`），只写一条
  `status="budget_exceeded"` 的 result 记录并回错误结果。

### ADD-057 工具轮 preamble 正文在两种模式下都**保留**（不变量）

网关最初的非流式实现把"工具轮同时返回的正文"丢弃（认为续接轮会重新生成），而流式实现
会把同一段正文实时 yield 出去。这会让"**已作为 `answer.delta` 发出的正文一定出现在最终
正文里**"这条不变量在两种模式间不一致（流式下用户看到的开头会凭空消失）。

统一为保留：两种模式都把工具轮的 preamble 计入最终正文，节点用 `answer_buffer` 作为
跨轮 `carried` 前缀拼接。续写轮**只**流出新增正文，不重复发 preamble。

### ADD-060 OpenAI 线上函数名不能带点号，网关做双向映射

§5.7 把工具名冻结成 `memory.search` / `memory.read`（rollout 契约 `MemoryToolName` 也是
这两个字符串）。但 Responses API 对**函数名**的限制是 `[A-Za-z0-9_-]{1,64}`——直接下发
带点的名字会被服务端 400。处置：线上下发 `memory_search` / `memory_read`，解析
`function_call` 时按 `MEMORY_TOOL_CANONICAL_NAMES` 映射回规范名，**图侧与 rollout 侧无感**
（`_SUPPORTED_TOOLS` 与 `MemoryToolName` 仍然只有带点的两个名字）。

### ADD-061 `open_answer_stream` 增加 `tool_rounds` 种子参数（冻结签名之外的唯一扩展）

冻结签名里 `AnswerTextStream.tool_rounds` 想表达"调用方已发生的工具轮数"，但每次续接都是
**新的流实例**，属性只能从 0 起算，上限校验落不了地。因此加了一个**有默认值**的可选参数
`tool_rounds: int = 0` 作种子（属性 = 种子 + 本轮是否真的请求过工具）；不传时行为与旧签名
完全一致，既有 Fake 网关不受影响。

### ADD-062 网关的 SDK 事实核对（`openai==2.53.0`）

| 冻结假设 | 实测 |
|---|---|
| `response.function_call_arguments.delta` 带 `name` | **不带**（只有 delta/item_id/output_index）。name 只在 `output_item.added` 与 `arguments.done` 上，因此按 `output_index` 归并 added/delta/done 三类事件，name 取 added/done，`.done` 的 arguments 作为权威值覆盖 delta 累积 |
| `strict` 工具 schema | 用 `strict=false`：strict 要求**全部字段必填且不接受 null**，与 memory 契约"省略即取默认值"直接冲突。JSON Schema 的 type/min/max/enum 与 `MemoryToolSearchRequest`/`MemoryToolReadRequest` 对齐，并有测试锁死同步关系 |

### OPEN-009 工具名为空参数时不判"不可执行"，错误由服务端兜住

网关只负责 JSON 解析：`arguments` 为空串按 `{}` 且 `executable=true`，因此模型对
`memory.search` 不给 `queries` 时不会在网关被拦下，而是打到 memory 服务端由 Pydantic
拒绝（422），再由图侧映射成 `MEMORY_TOOL_INVALID_ARGUMENT` 交回模型。要在网关就判
"必填缺失"，需要网关解释每个工具的参数语义——与"网关不解释业务"的边界冲突，暂不做。

### OPEN-010 `response.incomplete` / `response.failed` 终态事件不置 `truncated`（既有缺口）

既有流式实现只在 `event_type == "response.completed"` 且 `status == "incomplete"` 时置
`truncated`；SDK 里另有独立的 `response.incomplete` / `response.failed` 终态事件，真走到
那条路径时 `truncated` 会保持 False（少一个降级标记）。这是**改动前就存在**的缺口，
因"tools=None 零行为变化"的硬约束未在本 Phase 顺手修，登记待决。

### ADD-059 prime 的提示词载体是 `SnapshotMemory.prime`（可选字段）

§2.4 D1 要求"首轮 prime 注入（摘要 + 注册表目录）"，但没说它怎么进提示词。追代码发现
`build_answer_view` 的 `long_term_memory` 只投 **status + learner** 两个键——prime 拿到
手却**根本不会出现在模型输入里**（图和 memory 两侧都"实现完了"，提示词里却是空的）。

处置：`SnapshotMemory` 增加**可选**字段 `prime: dict`（默认空 dict），
`_memory_from_context` 从 `memory_context["prime"]` 取值，`build_answer_view` 仅在
`prime` 非空时挂 `long_term_memory["prime"]` 键：

- 关闭 prime 时视图 JSON **逐字不变**（不多一个空键），满足 §5.10 的"关闭即回滚"；
- prime 随快照进 checkpoint，resume 后提示词不变（已加往返单测）；
- 快照 `status` 只表达"注入内容是否被截断"：summary 缺失（当前无生产者）是**空状态**
  而不是内容退化，因此 status 仍为 `available`，可观测性由 `memory_prime_degraded`
  事件承担。否则在 Phase 7 落地生产者之前，所有用户的每一轮都会被标成 degraded。

### ADD-058 工具循环的进度事件复用 `stage="memory"`

§17.4.1 的 `turn.progress` 没有"记忆工具"专用阶段。工具循环复用了既有 `memory` 阶段
（`status=started/completed`），续写轮的 answer 阶段标题改为"正在结合记忆继续回答"，
避免前端看到两次"正在组织回答"。未新增 stage 取值（会改动事件契约）。

### ADD-052 服务端三个端点的 flag 门控与 §5.13 清单差异

见 ADD-048/ADD-049：端点始终挂载、read 不限制 memory_id 类型。两条都在 memory 侧
小节记录，此处只做索引。

### ADD-051 工具结果没有 `trace_id`（§5.7 要求，但无载体）

§5.7 要求"工具返回都带 trace_id、document_version 和 citation key"。`document_version`
与 citation key 有（read 响应带 version/checksum；rollout 的 `memory_tool_result` 记
`document_versions`），但 **memory 侧三个响应契约里没有 trace_id 字段**。轮次级关联
当前靠 conversation 的 `request_id`/`run_id`。补 trace_id 需要改 memory 侧响应契约，
未做——登记待决。

---

## Phase 6：`pending_batch` 与 nightly batch

（未开始）

---

## Phase 7：Consolidation、aliases、dangling links 与 KG 双路更新

（未开始）
