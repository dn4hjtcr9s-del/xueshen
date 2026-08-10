# MemoryManagerGraph 执行规格缺口审计索引

> **文档状态：已裁决，待需求方审阅**  
> **更新时间：2026-08-10**  
> **上位架构：** [`memorymangergraph.md`](./memorymangergraph.md)  
> **正式施工规格：** [`memory-manager-execution-spec.md`](./memory-manager-execution-spec.md)

本文把原始架构设计中需要补齐的执行问题改写为**已裁决的审计索引**。完整 Schema、DDL、Graph 节点、部署参数和测试要求只维护在正式施工规格中；本文不再重复粘贴第二套契约，避免实现时出现双重真相。

当前没有阻塞性的产品决策缺口。正式施工前需要需求方审阅的是三份文档之间的表达和验收口径，而不是重新填写技术选项。

---

## 1. 裁决总览

| 缺口类别 | 裁决结果 | 正式规格章节 | 状态 |
|---|---|---:|:---:|
| 项目基线与实施范围 | 完整第一版 Memory MVP；不实现对话 Agent/用户动态 Agent | §1 | [x] |
| 运行与部署 | 单台云服务器 + Docker Compose；PostgreSQL 持久任务队列 | §2、§14 | [x] |
| 认证与用户隔离 | 网站统一认证；Memory API 不登录；服务端注入用户身份 | §2、§18 | [x] |
| LangGraph 运行边界 | MemoryManagerGraph 仅为内部编排；外部通过 Gateway/MemoryClient | §10、§19 | [x] |
| OpenAI 调用 | OpenAI SDK + Responses API + Structured Outputs；成本/延迟优先 | §9 | [x] |
| Markdown 持久化 | MemoryService 唯一写入口；不可变版本 + DB 活动指针 + current 物化副本 | §8、§13 | [x] |
| 并发与幂等 | 用户级 advisory lock + expected_version + mutation/commit 幂等 | §4、§11、§13 | [x] |
| 低置信候选 | 不写活动 Markdown；进入候选区，用户可接受/纠正/拒绝 | §2、§6、§13、§19 | [x] |
| 总结记忆与图谱 | 独立存储、独立事务；Summary commit 通过 Outbox 派生 projection 更新 Overlay | §10、§15、§16 | [x] |
| 图谱可见状态 | 无状态、学习中、熟练、精通；用户不可手动设置精通 | §2、§16、§19、§20 | [x] |
| 检索 | 第一版使用 PostgreSQL `pg_trgm`；不引入向量库 | §3、§12 | [x] |
| 交付与验证 | 后端、API、Worker、Scheduler、Consumer、迁移、Compose、测试、GitHub Actions | §1、§14、§23、§24 | [x] |

---

## 2. 项目基线与第一版范围

### 2.1 已确认基线

- 当前仓库已有 React/Vite 前端原型，主要数据仍是 Mock。
- `knowledge_graph/` 已有固定知识图谱源文件。
- 当前没有可复用的 Memory 后端、数据库迁移、Memory Worker、Scheduler 或 Outbox Consumer。
- 不把对话 Agent、用户动态 Agent 的实现纳入本次施工；只定义它们未来接入 Memory 的 Reader/Client 接口和测试适配器。

### 2.2 本期必须交付

1. `backend/memory/` 模块及 FastAPI Gateway。
2. PostgreSQL DDL、Alembic migration、固定知识图谱只读注册表同步命令。
3. Markdown 版本存储、原子多文档提交和恢复/删除流程。
4. `MemoryService`、`KnowledgeGraphStateService`、`LearningContextService`。
5. `MemoryManagerGraph`、`SummaryMemoryGraph`、`KnowledgeGraphStateGraph`、Maintenance 分支。
6. PostgreSQL Worker、Scheduler、Outbox Consumer、Docker Compose。
7. Profile 和 KnowledgeMap 的最小真实 API 接入。
8. 单元、Graph、数据库/Markdown 集成、失败恢复、API 契约、前端测试和 GitHub Actions CI。

### 2.3 明确不做

- 不实现对话 Agent。
- 不实现用户动态 Agent、论坛后端、错题上传服务或行为采集系统。
- 不把 MemoryManagerGraph 暴露给浏览器，不暴露 LangGraph `threads/runs/checkpoints`。
- 不引入 Agent Server、MCP Server、Redis、消息队列、向量数据库或多机分布式部署。
- 不允许用户或模型修改固定图谱节点、标题、边和结构。

---

## 3. 技术栈和版本裁决

### 3.1 应用栈

| 项目 | 第一版选择 |
|---|---|
| Python | 3.13 |
| Web | FastAPI |
| Schema | Pydantic v2 |
| 数据访问 | SQLAlchemy async |
| PostgreSQL 驱动 | psycopg 3 async |
| 迁移 | Alembic |
| Graph | LangGraph 1.2.1 |
| Checkpointer | LangGraph Checkpoint 4.1.0 + PostgreSQL Checkpointer 3.0.4 |
| 模型客户端 | OpenAI SDK 2.38.0 |
| 模型 | 默认 `gpt-5.6-luna`，通过 `OPENAI_MEMORY_MODEL` 配置 |
| 结构化输出 | Responses API + Structured Outputs |
| 检索 | PostgreSQL `pg_trgm` |
| 测试 | pytest 及异步测试支持 |
| 部署 | Docker Compose |
| CI | GitHub Actions |

版本号是第一版的锁定基线，不等于允许实现者随意漂移的最低版本。升级必须通过依赖评估、迁移检查和完整 CI。

### 3.2 调用原则

- 成本和延迟优先，不流式，不自动升级到更昂贵模型。
- 正常每个 operation attempt 最多两次模型调用；任务生命周期最多四次。
- 模型只输出候选事实或受限计划草稿，不生成稳定 ID、当前版本、路径、SQL 或工具调用。
- 所有模型输出经过 Schema、业务策略、权限和版本校验后，才能交给 `MemoryService`。

---

## 4. 统一契约、ID 与幂等

### 4.1 `MemoryOperation`

正式契约定义在施工规格 §5–§7。关键裁决如下：

- 外部请求只提交公开 payload 和 `Idempotency-Key`；`operation_id`、`user_id`、`actor_type`、`priority`、`graph_thread_id` 由服务端注入或生成。
- `operation_type` 必须和 `payload.kind` 一一对应；`input_kind` 和优先级由代码推导，禁止调用方构造矛盾组合。
- `graph_thread_id` 固定为 `memory-op:{operation_id}`，只供内部 Graph 恢复使用。
- 幂等唯一键为 `(user_id, actor_type, idempotency_key)`，重复请求返回原 operation 的当前结果。
- `succeeded + review_candidate_ids` 表示低置信候选已安全暂存；只有阻塞性冲突才使用 operation 状态 `needs_review`。

### 4.2 稳定 ID 和版本

- `operation_id`、`mutation_id`、`commit_id`、`candidate_id` 均由应用代码/数据库生成，不由 OpenAI 模型生成。
- `candidate_id` 在候选落库时生成；模型只使用本次输出数组位置或证据引用。
- `mutation_id` 只为实际要提交的 mutation 生成。
- 用户命令中的 `expected_version` 是用户提交的并发令牌；`MutationPlanDraft` 不含该字段，应用代码读取当前活动版本后注入 `CommitMutationPlan.expected_version`。
- `no_change` 是策略结果，不创建 `mutation_id`、`memory_commits` 或引用不存在目标文档的 commit。
- `CommitMutationPlan` 在副作用节点前写入 LangGraph Checkpoint；重放复用原 `mutation_id`。版本冲突且尚未提交时废弃旧计划，并为重规划结果生成新 `mutation_id`。

### 4.3 用户命令内容类型

- `CorrectMemoryCommand.replacement` 使用 `LearnerReplacement | MasteryReplacement` 判别联合。
- `ReviewCandidateCommand.corrected_content` 使用同一受限联合。
- 服务端校验 replacement 类型、候选类型、目标 `memory_id` 和 topic 一致性。
- 任意 Markdown、JSON Patch、绝对路径、SQL 和文件删除命令都不是公开契约。

---

## 5. 总结记忆 Markdown 与版本存储

### 5.1 逻辑文档

```text
memory/index.md
memory/learner.md
memory/mastery/{topic_key}.md
```

- 总结记忆允许保存固定图谱之外的数学主题。
- `topic_key` 使用 Unicode 安全规范化算法；最终 key 由代码根据 `topic_title` 生成。
- `index.md` 是可重建派生文档，不是高并发读取的唯一真相。

### 5.2 活动版本协议

- 生产禁止人工直接编辑 Markdown。
- 历史版本不可变，数据库保存活动版本指针和 checksum。
- `current/` 目录是便于读取/备份的物化副本，不能反向成为写入口。
- 多文档 mutation 先生成 staged files，再在同一数据库事务中校验全部版本、写 commit、更新活动指针、更新索引并写 Outbox。
- 读请求以数据库活动指针为准；发现物化副本与指针不一致，由维护任务修复，不能静默改变活动版本。

### 5.3 删除、恢复和账号注销

- 单条记忆删除后立即不可见，30 天内可恢复。
- tombstone 不含正文；恢复正文放隔离区，恢复后以新活动版本重新物化。
- 旧证据不能重新创建同一已删除记忆；明确的新证据可以产生恢复/新版本。
- 账号注销事件到达后 24 小时内物理清理 Markdown、图谱 Overlay、Checkpoint 和未投递用户 Outbox。
- 备份最长 30 天淘汰。

---

## 6. Graph 与知识图谱 Overlay

### 6.1 父图和子图

```text
MemoryManagerGraph
├── SummaryMemoryGraph
├── KnowledgeGraphStateGraph
└── Maintenance 分支
```

Graph 只负责编排、提取、判断和生成受限计划；真正的 Markdown、数据库和 Overlay 写入都由服务层完成。

### 6.2 用户动作

| 用户动作 | Overlay 内部状态 | 前端显示 |
|---|---|---|
| `mark_unfamiliar` | `learning` | 学习中 |
| `mark_familiar` | `proficient` | 熟练 |
| `clear` | `null`/删除活动行 | 无状态 |
| 设置 `expert` | 拒绝，错误码 `GRAPH_STATUS_NOT_USER_SETTABLE` | 提示“精通由长期学习表现自动评估，不能手动设置” |

用户点击命令即时生效并写审计，但不永久锁定状态。后续总结证据必须通过独立 Outbox projection operation，且同时满足可靠节点映射、证据阈值和确定性状态转换规则，才可以调整 Overlay；变化要写 evidence refs，并发布 explanation 事件。

### 6.3 总结记忆到图谱的投影

- 总结记忆和图谱 Overlay 独立存储、独立提交、独立删除。
- Summary commit 成功后只写 `memory.changed` Outbox；Consumer 再创建 `project_summary_to_graph` operation。
- 来源版本必须仍是活动版本，且 node mapping 可靠；不满足时总结照常保存，图谱返回 `no_change`。
- 删除/纠正总结记忆时，不执行简单反向 delta，而是根据仍有效证据重算受影响节点。
- `expert` 只能由长期证据产生；普通单次浏览、收藏、打卡或单次错误不能形成掌握结论。

### 6.4 对外显示和解释

- API 统一返回 `status: null | learning | proficient | expert`，前端映射为“无状态/学习中/熟练/精通”。
- 用户点击 KnowledgeMap 节点时，如果状态被总结证据调整，前端读取 `graph_state.explanation_available` 并显示简短提示；默认不显示内部完整证据正文。
- 内部数据库可以保留证据快照、来源、版本、规则命中和审计，但不把这些内部字段当成额外公开状态。

---

## 7. Reader、API 和 Agent 接入边界

### 7.1 Reader

本期只实现 `ConversationReader`、`ActivityReader` 协议和测试适配器，不实现对话 Agent/用户动态 Agent。Reader 只能按授权读取引用的外部内容，不读取对方数据库表，不把完整对话或论坛正文复制进 Memory 数据库。

### 7.2 外部接口

- 浏览器使用网站统一认证后的 BFF/后端接口；不能伪造 `user_id`。
- 对话 Agent 和用户动态 Agent 通过 `MemoryClient` 提交 evidence。
- KnowledgeMap 通过 Overlay API 提交 `mark_unfamiliar`、`mark_familiar`、`clear`。
- 其他读取方使用 `LearningContextService`、summary search 和 graph recommendations。
- 第一版不提供 MCP；如果未来需要，只在 `MemoryClient` 之上增加业务适配器，不能暴露写文件、删文件或直接 commit 工具。

正式 HTTP 和 Client 契约见施工规格 §18–§20。

---

## 8. 运行、存储和可靠性

施工规格已经裁决以下实现：

- PostgreSQL 持久化 operation、版本、索引、审计、Checkpoint 和 Outbox。
- Worker 使用 Lease、心跳、至少一次执行和有限重试。
- MemoryService 采用用户级 advisory lock、按 memory/node 排序加锁、expected_version 校验和 commit 幂等。
- Outbox 与核心提交同事务写入，由 Consumer 重试发布。
- fixed graph 注册表只读，只有同步命令可以从仓库源文件替换节点和边。
- `pg_trgm` 作为第一版检索方案，不建立向量库。

详细 DDL、任务状态机、环境变量和启动命令见施工规格 §11–§15。

---

## 9. 前端最小接入

### 9.1 Profile

Profile 显示 `learner.md` 和受控的 summary search 结果；支持用户纠正、删除、恢复和候选审核，不显示内部 Markdown 路径、模型 reasoning 或完整证据正文。

### 9.2 KnowledgeMap

KnowledgeMap 读取固定图谱和用户 Overlay，显示四种状态。用户点击“不熟悉/熟悉/清除”立即提交确定性命令；点击或尝试设置“精通”时显示系统提示，而不是让前端发送未支持的状态枚举。

### 9.3 Operation 轮询

异步 operation 只返回业务状态、版本和公开错误；前端通过 `operation_id` 轮询，不接触 LangGraph thread/checkpoint。

---

## 10. 最终审计检查

实现前必须以施工规格为唯一执行清单，并验证：

- 契约中的枚举、payload kind、API 和数据库 CHECK 一致。
- `MutationPlanDraft` 不包含稳定 ID 和由数据库读取决定的 `expected_version`；只有 `CommitMutationPlan` 包含应用代码注入的提交字段。
- 低置信候选不污染活动 Markdown；阻塞冲突才进入 `needs_review`。
- `no_change` 不产生无目标 commit。
- 图谱本体不能由业务 API 改变；Overlay 只返回四种公开状态。
- Summary 与 Graph 的更新通过 Outbox 弱连接，而不是同一事务强绑定。
- 账号删除、管理员 break-glass、日志脱敏和备份淘汰规则可测试。

详细执行顺序和验收清单见施工规格 §23–§25。

---

## 11. 原设计与执行规格的关系

- 本文是架构语义和边界说明。
- [`memory-manager-execution-spec.md`](./memory-manager-execution-spec.md) 是第一版施工基线。
- 本文中的旧示例若与施工规格冲突，以施工规格为准；修改后，两份文档应保持同一公开状态枚举和同一提交边界。
- [`memory-manager-execution-spec-gap-analysis.md`](./memory-manager-execution-spec-gap-analysis.md) 只作为缺口裁决索引，不再作为决策问卷。

---

## 12. 最终决策清单

```text
[已确定] 项目架构固定为 LangGraph + OpenAI SDK
[已确定] 短期记忆由对话 Agent Checkpointer 保存
[已确定] 总结记忆保存为开放式 Markdown
[已确定] 知识图谱本体只读，用户只修改个人 Overlay
[已确定] 总结记忆和知识图谱记忆独立维护、弱连接
[已确定] MemoryManagerGraph 是内部处理引擎
[已确定] 前端和其他 Agent 通过 Gateway / MemoryClient 访问
[已确定] 现阶段不引入 Agent Server
[已确定] Graph 调用边界为 MemoryGraphRunner.run(operation)
[已确定] 任务采用 PostgreSQL 持久任务表和 Lease
[已确定] 任务采用至少一次执行，提交采用幂等
[已确定] LangGraph Checkpoint 用于 Graph 恢复，不代替长期记忆
[已确定] MemoryService 是唯一的持久化写入入口
[已确定] Markdown 使用版本化和原子提交
[已确定] 索引和通知通过 Outbox 异步补偿
[已确定] 用户图谱命令即时生效但不永久锁定；总结证据可通过独立 projection 调整
[已确定] 图谱对外只显示无状态、学习中、熟练、精通；用户不能手动设置精通
[已确定] 稳定 ID 和 expected_version 由应用代码/数据库读取结果生成或注入
[已确定] no_change 不创建稳定 mutation 或不存在目标文档的 commit
```

核心职责边界最终归纳为：

> **Gateway 负责接收和鉴权；Scheduler/Worker 负责调度和恢复；Runner 负责隔离 Graph 运行位置；MemoryManagerGraph 负责编排和语义判断；OpenAI SDK 负责结构化提取与总结；MemoryService 负责真正修改；Markdown 保存总结记忆事实；PostgreSQL 管理任务、版本、幂等和索引；固定知识图谱只提供参考结构；其他 Agent 只能通过 MemoryClient 读取或提交事件。**
