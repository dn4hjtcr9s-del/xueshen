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

（实施中，条目随开发追加）

---

## Phase 2：Manifest、Reader、恢复与删除

（未开始）

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
