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

（未开始）

---

## Phase 4：长期记忆 Markdown schema v2

（未开始）

---

## Phase 5：Memory Prime 与 `memory.search` / `memory.read`

（未开始）

---

## Phase 6：`pending_batch` 与 nightly batch

（未开始）

---

## Phase 7：Consolidation、aliases、dangling links 与 KG 双路更新

（未开始）
