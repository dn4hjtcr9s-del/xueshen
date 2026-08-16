# Conversation Agentic RAG 待决项清单

> 日期：2026-08-13
> 使用方式：每项给出"我的建议"，你可以直接回复"全部按建议"或逐条修改。
> 标注 **[阻断]** 的项目不定下来就不能开始对应 Phase。
> 配套文件：`docs/conversation-gap-analysis.md`（核对依据）

---

## 第一组：流程与范围

### D1. Memory 评审结论的当前状态 [阻断 Phase 8，建议 Phase 0 前澄清]

`Review 结论.txt` 为 Request changes（1 P0 + 4 P1），但代码抽查显示这些问题疑似
已修复（fencing CAS、purge 分阶段、MaintenanceGate、JWT 严格校验均已落地）。

- 问题：这些修复是否已通过复审？还是评审文档已过时？
- **我的建议**：你确认一下状态。若有未修复项，列出来；与 Conversation 无关的
  （如备份恢复）不阻断本方案开发，只阻断上线。

### D2. Graph Runner / Outbox Publisher / 摘要任务的部署形态 [阻断 Phase 2/5]

方案未定义。参照现有四进程模式（api/worker/scheduler/outbox-consumer）：

- **我的建议**：
  1. 新增 `conversation-worker` 容器：`uv run python -m backend.conversation.worker.main`，
     进程内跑 Graph Runner（DB 轮询 `accepted` turn 唤醒，间隔 ~1s）+ Outbox
     Publisher + 摘要/标题异步任务，三合一，与 memory-worker 模式一致；
  2. 取消信号：API 写 `conversation_turns.status='cancelling'`，Runner 每个节点
     边界检查该标志（配合 LangGraph interrupt/取消），不引入额外通道；
  3. 显式记忆的同步投递尝试（§16.4.3）由 finalize 节点在 worker 内直接执行，
     不经过 API 进程；
  4. 第一版单副本 worker，同 thread 串行由 DB 部分唯一索引保证。

### D3. Conversation API 挂在哪个应用 [阻断 Phase 6]

- **我的建议**：挂进现有 `backend/app.py` 同一 FastAPI app（同 uvicorn 进程、
  同容器），新增 `/api/v1/conversations` router。理由：复用认证依赖、统一
  PublicError 模型、maintenance gate、CORS、metrics；readiness 增加第三条
  alembic 链检查。前端 vite proxy 增加 `/conversation-api` 前缀（仿 `/memory-api`）。
- 备选：独立 app/容器。仅当你预期 conversation 需要独立扩缩容时选。

### D4. Settings 组织方式

方案 §20 建议独立 `ConversationSettings`，但现状是单一 `Settings` 类
（`backend/settings.py`）按前缀分组。

- **我的建议**：维持单一 Settings 类，新增 `conversation_*` 字段组
  （与 `memory_*` 同模式），不拆类。与现有工程惯例一致，装配简单。

### D5. Source deletion 的传输通道 [阻断 §8.6，需授权改动 Memory 域]

Memory 侧无接收 `SourceDeletedEvent` 的 HTTP 端点，Handler 未接线。

- **我的建议**：在 Memory API 新增 `POST /api/v1/internal/source-deletions`
  （内部 scope 鉴权，仿 internal.py purge 端点），调用既有
  `RecordingSourceDeletionHandler`；同时在两个 composition root 把
  `DeletionAwareConversationReader` 包到真实 Reader 外层。这是对 Memory 域的
  **新增端点**，超出方案"只替换依赖注入"的边界，需要你授权。

### D6. 账号删除的跨域编排 [阻断 §8.6.7]

- **我的建议**：第一版由运维/序列化流程保证：账号注销流程先调 Conversation
  删除接口（清理 conversation DB + outbox + events + checkpoint），再调 Memory
  purge 内部 API。不建自动编排器；在 Runbook 中固化顺序与失败补偿。
  若你希望自动化，需要指定编排方（建议 auth_service 的账号删除流程）。

### D7. 语料权限模型

RetrievalService 只有内容维度过滤，无用户/租户级语料权限；SQL 已内置
active corpus 限定。

- **我的建议**：第一版单语料、全体用户共享 active corpus；`semantic_filters`
  白名单 = SearchFilters 五维度。多租户权限过滤列入非目标。

---

## 第二组：契约与默认值

### D8. 配置默认值批量确认 [阻断 Phase 0 契约冻结]

方案给了名字的项，我建议默认值如下（§14.3 已有值的不重复列出）：

| 配置 | 建议值 | 依据 |
|---|---|---|
| `CONVERSATION_CONTEXT_MAX_MESSAGES` | 20 | 有界最近消息 |
| `CONVERSATION_CONTEXT_TOKEN_BUDGET` | 6000 | 历史+摘要合计 |
| snapshot `memory_tokens` | 3000 | 直接透传 LearningContext token_budget 默认值 |
| snapshot `retrieval_tokens`（= `EVIDENCE_TOKEN_BUDGET`） | 4000 | 证据集预算 |
| snapshot `answer_tokens` | 2000 | 回答最大输出 |
| `CONVERSATION_TURN_DEADLINE_SECONDS` | 120 | 覆盖 10s 检索 + 长回答 |
| `CONVERSATION_RETRIEVAL_CONCURRENCY` | 4 | = 最大子问题数 |
| `CONVERSATION_RETRIEVAL_RESULT_LIMIT` | 20 | hybrid_search 默认 limit |
| `CONVERSATION_SUMMARY_TRIGGER_TOKENS` | 8000 | 超出则触发摘要任务 |
| `SSE_HEARTBEAT_SECONDS` | 15 | 常规反代空闲超时之下 |
| `SSE_EVENT_RETENTION_DAYS` | 30 | 与 memory 通知保留同级 |
| `SSE_DELTA_BATCH_MS` | 100 | 流式顺滑度与行数平衡 |
| `SSE_DELTA_BATCH_CHARS` | 64 | 同上 |
| `MEMORY_CONTEXT_TIMEOUT_SECONDS` | 5 | 不阻塞主流程 |
| Outbox：poll / lease / max_attempts | 1s / 60s / 10 | 对齐 memory_outbox_* 现值 |

### D9. `source_checkpoint_id` 语义 [阻断 Phase 0]

- **我的建议**：定义为 **turn 完成时的消息快照标识**：
  `conv-src-v1:{thread_id}:{assistant_message_sequence}:{sha256(user_msg.content_hash + assistant_msg.content_hash) 前16位}`。
  Reader 校验时重算：指定 message_ids 的 content_hash 组合与 checkpoint_id 中
  的哈希一致才返回（满足 §8.3.5"版本匹配"）；消息被编辑/删除后哈希不匹配，
  返回 SOURCE_DELETED。纯 Conversation 侧定义，不触碰 Memory 契约。

### D10. SSE 认证方式 [阻断 Phase 6]

EventSource 无法带 Authorization header。

- **我的建议**：一次性 stream ticket——`POST /turns` 响应和 `GET /turns/{id}`
  返回短期（60s）一次性 `stream_ticket`，SSE 连接走
  `GET .../events?ticket=...`；ticket 服务端一次性核销，与用户会话绑定。
  不引入 cookie 改造，不泄漏 access token 到 URL 日志（ticket 短命且一次性）。
- 备选：`@microsoft/fetch-event-source` 库用 fetch 实现 SSE 可带 header——
  引入新依赖，但避免 ticket 机制。**其实更省事**，二选一请你定。

### D11. RewritePlan / EvidenceAssessment 细粒度字段

- **我的建议**：
  - `semantic_filters` 契约直接复用 SearchFilters 五字段（book_ids/grade_levels/
    sections/content_roles/chapter_prefix），服务端白名单校验后透传；
    具体词汇枚举从 RAG DB 现存数据取样后在 Phase 0 写入契约文档；
  - `answer_mode × need_retrieval` 合法组合：`(direct, false)`、
    `(memory_assisted, false)`、`(rag, true)` 三种，其余组合服务端纠正为
    `need_retrieval=true → rag`；
  - Follow-up：最多 3 条，由 answer 同一次调用的 Structured Output 附带生成
    （`AnswerPayload.followups`），失败则省略，不单独重试；
  - 会话标题：首个 turn 完成后由摘要任务用 `OPENAI_CONVERSATION_SUMMARY_MODEL`
    生成，失败兜底为用户消息前 20 字。

### D12. SSE 各事件 data payload

- **我的建议**：Phase 0 由我按 §7.4 事件列表逐一定义 Pydantic 模型并提交你审，
  原则：只含前端渲染必需字段（delta 文本、citation DTO、状态码、degraded flags），
  不含任何内部诊断。

### D13. 摘要/标题任务触发机制

- **我的建议**：并入 conversation-worker（见 D2），按
  `SUMMARY_TRIGGER_TOKENS` 阈值在 turn 完成后检查并执行；摘要失败降级为有界
  最近消息（方案 §7.6 已规定）。

---

## 第三组：模型、评测与工程

### D14. 四个角色模型的具体模型名

- **我的建议**：第一版统一用现有 `OPENAI_BASE_URL` 指向的网关，rewrite/evidence/
  summary 用 `gpt-5.6-luna`（与 memory 一致），answer 用支持流式的同族模型
  （若网关只有一个可用模型则全部用它）。具体值你定或授权我按"全部复用
  `OPENAI_MEMORY_MODEL` 同款"执行。

### D15. Query Embedding 提供方配置

- 现状：`text-embedding-v4`、1024 维（DashScope 系）。生产 query embedding 的
  base_url/key 环境变量未在任何配置中。
- **我的建议**：新增 `CONVERSATION_EMBEDDING_BASE_URL` /
  `CONVERSATION_EMBEDDING_API_KEY` / `CONVERSATION_EMBEDDING_MODEL=text-embedding-v4`，
  运行时校验维度=1024 且与 active corpus manifest 一致。请确认 key 来源
  （是否复用现有某个环境变量）。

### D16. 评测门槛数值 [阻断 Phase 8 验收，可后置到 Phase 7]

- **我的建议**：先不定死，Phase 0 只在契约中预留评测指标定义；门槛数值等
  离线数据集建好后（Phase 7）用基线跑一轮再定。数据集构建：`evals/` 目录
  现有资产 + 人工构造 50–100 条多轮问题，由我起草、你抽样审。

### D17. 测试基础设施

- **我的建议**：集成测试用 testcontainers（mypy overrides 已出现该包，说明
  仓库已有先例）；OpenAI/Embedding 一律 Fake，不进 CI 真实调用；前端用
  msw + playwright（已装）。

### D18. Git 工作流

- **我的建议**：每个 Phase 一个 commit 序列直接落在当前分支（或你指定分支），
  每 Phase 到达验收门槛即停下汇报，等你确认再进下一 Phase。commit message
  风格我参照仓库现有 git log（执行前我会先看）。

---

## 裁决速查表

| 编号 | 主题 | 我的建议一句话 |
|---|---|---|
| D1 | Memory 评审状态 | 请你确认修复是否已闭环 |
| D2 | Runner 部署形态 | 三合一 conversation-worker 容器，DB 轮询唤醒 |
| D3 | API 归属 | 同 app 加 router |
| D4 | Settings | 单一类加 conversation_* 前缀 |
| D5 | Source deletion 通道 | Memory 加内部端点（需授权） |
| D6 | 账号删除编排 | 运维序列化流程 + Runbook |
| D7 | 语料权限 | 第一版单语料全局共享 |
| D8 | 配置默认值 | 按上表 |
| D9 | source_checkpoint_id | 内容哈希快照标识 |
| D10 | SSE 认证 | 一次性 ticket 或 fetch-event-source 二选一 |
| D11 | RewritePlan 细节 | 按建议组合矩阵与白名单 |
| D12 | SSE payload | Phase 0 我定义你审 |
| D13 | 摘要任务 | 并入 worker，阈值触发 |
| D14 | 角色模型 | 复用现有网关模型 |
| D15 | Embedding 配置 | 新增三个环境变量 |
| D16 | 评测门槛 | 后置到 Phase 7 |
| D17 | 测试设施 | testcontainers + Fake |
| D18 | Git 工作流 | 每 Phase 停在验收门槛 |
