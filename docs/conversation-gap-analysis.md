# Conversation Agentic RAG 方案 vs 代码现状 差距清单

> 日期：2026-08-13
> 依据文档：`docs/conversation-agentic-rag-implementation-plan.md`（下称"方案"）
> 核对方式：只读核对，未修改任何代码
> 配套文件：`docs/conversation-decision-items.md`（待决项清单）

---

## 1. 核对结论总览

方案的**架构前提与现有代码高度吻合**：MemoryClient 接口、ConversationReader Protocol、
`_UnavailableConversationReader` 注入点、Outbox 模式、AsyncPostgresSaver checkpointer、
Agent 委托认证、SourceBundle 上限，均与方案描述一致。方案可以执行。

真正的差距集中在五类：

| 类别 | 数量 | 阻断级别 |
|---|---|---|
| A. 方案前提与现状有出入（需修正方案认知） | 5 | 中 |
| B. 方案未定义的架构决策 | 4 | **高（动手前必须定）** |
| C. 契约语义缺口（Phase 0 冻结时必须补齐） | 9 | 高 |
| D. 需要触碰方案边界外代码的改动 | 3 | **高（需授权）** |
| E. 运维/评测/流程缺口 | 5 | 中 |

---

## 2. A 类：方案前提与现状的出入

### A1. Memory 评审状态与方案描述不一致

- 方案 §2.1 称 MemoryManagerGraph "已构建完成并处于 Review"。
- 实际：`Review 结论.txt` 为 **Request changes**（第二轮：1 个 P0 + 4 个 P1）。
- 但抽查代码显示第二轮问题**疑似已修复**：
  - commit marker fencing：`backend/memory/persistence/operations.py:189-213` 已加
    `expected_worker/expected_generation` CAS；
  - purge 自身 checkpoint：`backend/memory/services/account_purge.py` 已改为分阶段、
    purge 自身 operation 保留到阶段 3；
  - restore 流量隔离：`backend/memory/backup.py` 已引入 `MaintenanceGate`；
  - JWT 严格校验：`backend/auth/verifier.py` 已强制 actor_type/scopes/delegated_sub
    （评审 #4/#15）；SourceBundle 重算（评审 #12）也已落地。
- **影响**：评审文档与代码状态脱节。若修复未经复审确认，方案 Phase 8 的上线门槛
  缺乏基线。→ 待决项 D1。

### A2. SourceBundle 上限数值其实已冻结，方案 §8.3.10 不需要新定

- 方案 §8.3.10 说"单项长度、总 80KB、metadata 大小和条目数限制"待定。
- 实际 `backend/memory/contracts/evidence.py` 已冻结：总 80KB、单项 20KB、
  items ≤ 200、metadata ≤ 4KB 且 ≤ 50 keys，且 total_utf8_bytes 强制重算。
- **影响**：方案应直接引用这些常量，无需新决策。Reader 只需遵守。

### A3. RAG 侧 §12.5 "最小增强"比方案预期更小

- `rag_migrations/versions/0001_rag_core.py` 显示 `rag.chunks` **已有**
  `corpus_id`、`chunk_index`、`token_count` 列（且 `UNIQUE(corpus_id, chunk_index)`）。
- 增强仅需：`_SELECT_COLUMNS` 增加三个字段 + `SearchHit` dataclass 增加字段 +
  `from_mapping` 映射。**无 schema 变更**。
- 另一个好消息：检索 SQL 已内置 active corpus 过滤
  （`c.corpus_id = (SELECT corpus_id FROM rag.corpus_versions WHERE status='active')`，
  `backend/rag/retrieval.py:176` 等三处）。方案 §11.2.6 的"active corpus 由服务端
  注入"**已被现有实现满足**。
- 但仍存在：多租户/用户级语料权限过滤在 RetrievalService 中**完全不存在**
  （SearchFilters 只有 book_ids/grade_levels/sections/content_roles/chapter_prefix
  五个内容维度）。→ 待决项 D7。

### A4. `checkpoint_id` 在 Memory 侧只是不透明字符串

- Memory 规格 §6.1/§17.1 与代码均将 `checkpoint_id` 视为 `str | None`（≤200 字符），
  语义完全由对话系统自定义。
- 因此方案 §7.2 `source_checkpoint_id` 的定义权和 §8.3.5"版本匹配"算法
  **完全在 Conversation 侧**，不违反 Memory 契约，但方案没有给出具体定义。
  → 待决项 D9。

### A5. 前端 SSE 认证是真实障碍，方案未提及

- 现有认证：内存中的 Bearer access token + HttpOnly refresh Cookie
  （`frontend/src/api/client.ts`）。
- 浏览器原生 `EventSource` **不能设置 Authorization header**。
- 方案 §17 全部 SSE 设计都默认"已认证连接"，但未说怎么认证。→ 待决项 D10。

---

## 3. B 类：方案未定义的架构决策（动手前必须定）

### B1. Graph Runner 部署形态（方案全文未定义）

- 方案 §5.1 "提交事务后启动或唤醒 Graph Runner"，§24 有 `worker/graph_worker.py`，
  但没有说明：进程内 asyncio task / 独立 worker 进程 / 队列驱动？如何唤醒？
  取消信号如何传播到运行中的 Graph？多副本时同一 turn 由谁执行？
- 现状参考：docker-compose 已有 memory-api / memory-worker / memory-scheduler /
  memory-outbox-consumer 四进程模式可复用。→ 待决项 D2。

### B2. Conversation API 与现有 FastAPI app 的关系

- `backend/app.py` 是 Memory + Auth 的单体入口（含 maintenance gate、统一错误模型、
  health/ready 检查 memory/auth 两条 alembic 链）。
- 方案 §24 目录是 `backend/conversation/`，暗示同仓库，但未说明：
  同一 FastAPI app 加 router，还是独立 app / 独立 uvicorn 进程 / 独立容器？
- 影响：认证中间件复用、maintenance gate 覆盖范围、readiness 扩展、CORS、
  vite proxy 前缀。→ 待决项 D3。

### B3. Outbox Publisher 部署形态

- 同 B1：publisher 是独立容器（仿 memory-outbox-consumer）还是并入 graph worker？
  显式记忆的"同步投递尝试"（方案 §16.4.3）由 API 进程还是 publisher 进程执行？
  → 并入待决项 D2。

### B4. 摘要与会话标题的异步任务机制

- 方案 §7.6 说"由异步摘要任务维护"、§7.1 标题"可异步生成"，但没有机制定义：
  独立 worker？turn 完成后在 graph 内触发？轮询表？
  → 并入待决项 D2 或单列 D13。

---

## 4. C 类：契约语义缺口（Phase 0 冻结前必须补齐）

| # | 位置 | 缺口 |
|---|---|---|
| C1 | §7.2 / §8.3.5 | `source_checkpoint_id` 的生成者、格式、与 message 内容的版本匹配算法 |
| C2 | §17.4–17.5 | 每种 SSE event_type 的 `data` payload 结构（全部 9 种事件） |
| C3 | §15.5 | Follow-up：数量上限、生成方式（answer 同一调用内 or 额外调用）、失败降级 |
| C4 | §11.1 | `semantic_filters` 值域：SearchFilters 的五个维度已存在，但 grade_levels /
 sections / content_roles 的**实际词汇表**需从 RAG 数据中确认枚举 |
| C5 | §11.1 | `answer_mode`(direct/memory_assisted/rag) × `need_retrieval` 的合法组合矩阵 |
| C6 | §9.2 / §20 | 快照四项 token 预算 + 全部无默认值配置项的默认数值（见待决项 D8 清单） |
| C7 | §7.4 | answer.delta 聚合窗口（毫秒/字符数）默认值 |
| C8 | §7.1 | 会话标题生成：模型、触发时机、失败兜底文案 |
| C9 | §25 Phase 8 | 评测发布门槛数值（Recall@K、答案忠实性等目标值） |

---

## 5. D 类：需要触碰方案边界外代码的改动（需授权）

### D-a. Memory API 缺少 source deletion 接收端点（阻断 §8.6）

- 现状：`RecordingSourceDeletionHandler`（`backend/memory/readers/handler.py`）与
  `DeletionAwareConversationReader` **存在但未在任何 composition root 接线**；
  Memory API 没有接收 `SourceDeletedEvent` 的 HTTP 路由（`api/internal.py` 只有
  account purge）。
- 方案 §8.6.2 说 Publisher 调用"基于既有 SourceDeletionHandler 的 Gateway"，
  但 transport 不存在。要么 Memory API 新增内部端点（改动 Memory 域，超出方案
  §2.2"只替换依赖注入"的边界），要么 publisher 进程直插 Memory DB（违反数据库
  隔离边界 §2.2.1）。→ 待决项 D5。

### D-b. 账号删除的跨域编排（阻断 §8.6.7）

- Memory 侧 `purge_account_memory` 已实现（内部 API）。
- 方案 §8.6.7 要求"账号删除同时处理 Conversation 数据、未投递 Outbox、SSE 事件、
  Conversation Checkpoint 和 Memory 账号清理流程"，但**编排方未定义**：
  是 auth_service 删除账号时调用 Conversation 清理 + Memory purge？
  顺序、失败补偿、部分完成的语义全部未定。→ 待决项 D6。

### D-c. `backend/app.py` 的 readiness 需扩展

- 若 Conversation 与 Memory 同 app（待决项 D3），`/health/ready` 目前只检查
  memory/auth 两条迁移链，需要加 conversation 链检查；若同进程跑 graph runner，
  maintenance gate 语义也要明确是否覆盖 conversation 写路径。→ 并入 D3。

---

## 6. E 类：运维 / 评测 / 流程缺口

| # | 缺口 | 说明 |
|---|---|---|
| E1 | 评测数据集来源 | §26.6 要求"版本化离线数据集"，构建方式、规模、标注者未定；现有 `evals/` 目录是否有可复用资产需确认 |
| E2 | Query Embedding 调用配置 | 现有 query 向量只在 `scripts/rag_verify.py` 产出；embedding 模型为 `text-embedding-v4`（1024 维，DashScope 系）；生产 query embedding 的 base_url/key 环境变量名未在任何配置中出现 |
| E3 | OpenAI 兼容网关 | docker-compose 显示 `OPENAI_BASE_URL` 可指向 DeepSeek；方案四个角色模型的具体模型名需定（现有 memory 用 `gpt-5.6-luna`） |
| E4 | CI 集成测试基础设施 | `pyproject.toml` mypy overrides 出现 testcontainers，但未见 CI 配置；docker-compose 还是 testcontainers 需定 |
| E5 | Git 工作流 | 分支策略、提交粒度、是否需要逐 Phase review  checkpoint |

---

## 7. 已验证"无需决策"的事实（供放心引用）

1. `MemoryClient.submit_conversation_evidence/build_learning_context/search_summary/
   get_graph_recommendations` 签名与方案 §2.1/§16 一致。
2. `ConversationReader` Protocol 签名与方案 §8.1 一致；`_UnavailableConversationReader`
   注入于 `backend/app.py:117` 与 `backend/memory/worker/main.py:103`，与方案 §8.5
   描述的两个装配点一致。
3. `LearningContext` 含 learner/mastery/graph_states/recommendations/truncated，
   与方案快照 §9.2 memory 段结构吻合；`token_budget` 参数（500–8000，默认 3000）
   可直接承载方案 memory_tokens 预算。
4. `RetrievalService` 确为同步 SQLAlchemy（`def hybrid_search`），方案 §12.2 的
   Async Adapter 判断正确；目前仅 `scripts/rag_verify.py` 调用它，无生产调用方。
5. `SearchFilters` 五维度（book_ids/grade_levels/sections/content_roles/
   chapter_prefix）可作为方案 §11.1 `semantic_filters` 的服务端校验白名单。
6. 依赖版本与方案 §2.1 一致：langgraph==1.2.1、langgraph-checkpoint-postgres==3.0.4、
   openai>=2.38、FastAPI、Pydantic v2、SQLAlchemy 2、pytest/ruff/mypy 已配置。
7. 三条 alembic 链并存（`alembic.ini`=memory、`auth_alembic.ini`=auth、
   `rag_alembic.ini`=rag）已是仓库惯例，方案新增第四条 conversation 链模式一致。
8. 前端已装 react-markdown/remark-math/rehype-katex/katex、msw、vitest、playwright；
   无路由库、无状态管理库（App.tsx 用 useState 切换页面）；vite proxy 已配
   `/memory-api` 前缀并注入 Dev Auth header，conversation 可加平行前缀。
9. 删除语义：`RecordingSourceDeletionHandler` 幂等键与 `SourceDeletedEvent` 契约
   已按规格 §17.3 实现，conversation 侧只需产生正确的事件。
