# xueshen-math

> English version: [README.en.md](./README.en.md)

**MemoryManagerGraph** —— 面向数学学习的长期记忆与知识图谱系统。它从对话、练习与社区活动中异步提取可解释的学习记忆，维护每个用户的知识图谱掌握状态，并提供 Agentic RAG 数学答疑对话、错题本复习与学习社区服务。

## 功能

- **长期记忆**：从对话证据异步提取 → 审核 → 合并为 Markdown 记忆，支持纠错、删除、恢复与图谱标记
- **知识图谱**：固定教材知识图谱 + 用户级熟悉度状态（不熟悉 / 熟悉 / 精通），状态可解释、有依据
- **智能对话**：Agentic RAG（多查询检索 + 证据循环），SSE 流式输出，回答带引用
- **错题本**：一键收藏 AI 对话中的题目，按间隔重复规则复习
- **学习社区**：讨论区 / 学习小组 / 打卡圈（feature flags 灰度，默认关闭）
- **注册登录**：内嵌 JWT 认证服务；开发模式支持免登录体验

## 技术栈

- **后端**：Python 3.13 · FastAPI（单进程多域）· SQLAlchemy 2 + Alembic（五条独立迁移链）· LangGraph worker · PostgreSQL 17
- **前端**：React 19 · TypeScript · Vite · KaTeX（公式渲染）
- **工程**：uv · Ruff · mypy · pytest · Vitest · Playwright · Docker Compose

## 快速开始

### 前置要求

- Python 3.13（由 [uv](https://docs.astral.sh/uv/) 管理）
- Node.js 20+ / npm
- Docker（含 Compose 插件）

### 本地开发启动

```bash
# 1. 安装依赖（dev 含 pytest/ruff/mypy；ocr 含 pypdf）
uv sync --extra dev --extra ocr

# 2. 配置环境变量（.env 不入库）
cp .env.example .env

# 3. 启动本地 PostgreSQL（注意端口是 55432，非默认 5432）
docker compose up -d --wait postgres

# 4. 依次迁移各域数据库链，并同步知识图谱注册表
uv run alembic upgrade head                                  # memory 链
uv run alembic -c auth_alembic.ini upgrade head              # auth 链
uv run alembic -c conversation_alembic.ini upgrade head      # conversation 链
uv run alembic -c community_alembic.ini upgrade head         # community 链
uv run python -m backend.memory.cli sync-knowledge-graph --apply

# 5. 启动 memory-api（唯一入口，按配置条件挂载 conversation/community 路由）
uv run uvicorn backend.app:app --port 8000

# 6. 另开终端启动后台进程
uv run python -m backend.memory.worker.main           # 记忆处理 worker（LangGraph）
uv run python -m backend.memory.worker.scheduler      # 维护调度
uv run python -m backend.memory.worker.outbox_consumer
uv run python -m backend.conversation.worker.main     # 对话 worker
uv run python -m backend.conversation.publisher.main  # 对话 outbox 发布

# 7. 启动前端
cd frontend
npm install
npm run dev
```

### 验证

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5173 |
| API 文档（OpenAPI） | http://localhost:8000/docs |
| 就绪检查 | http://localhost:8000/health/ready |

开发模式默认开启 Dev Auth（`DEV_AUTH_ENABLED=true`）：Vite 代理会把 `frontend/.env` 中 `MEMORY_DEV_USER_ID` 注入 `X-Dev-User-Id` 头，访客免登录即可浏览。调试 API 也可手动携带：

```bash
curl -H "X-Dev-User-Id: 00000000-0000-4000-8000-000000000001" \
  http://localhost:8000/health/ready
```

### Docker 一键启动

```bash
docker compose up --build            # postgres + memory-api(127.0.0.1:8001) + 各 worker
docker compose --profile frontend up # 追加前端预览（127.0.0.1:4173）
```

### RAG（可选）

RAG 使用完全独立的 PostgreSQL 库（端口 55433）：

```bash
docker compose -f docker-compose.rag.yml up -d --wait rag-postgres
uv run alembic -c rag_alembic.ini upgrade head
```

## 项目结构

```
xueshen-math/
├── backend/
│   ├── app.py                  # FastAPI 唯一入口（按配置挂载各域路由）
│   ├── memory/                 # 核心长期记忆域：API → operation 队列 → LangGraph worker
│   │                           #   → 事务化提交 + outbox；正文存 Markdown，索引/事件存 PostgreSQL
│   ├── auth_service/           # 内嵌 JWT 签发方（与 memory-api 同进程）
│   ├── auth/                   # JWT 验签方
│   ├── conversation/           # Agentic RAG 对话域（独立库 + worker/publisher，SSE 流式）
│   ├── community/              # 社区域（独立库；未配置 COMMUNITY_DATABASE_URL 时不挂载路由）
│   ├── rag/                    # RAG 导入/检索（独立 rag 库，被 conversation 检索复用）
│   └── settings.py             # 环境变量集中定义
├── frontend/                   # React 19 + Vite + TypeScript
├── tests/                      # unit / integration / contract / conversation / community / rag / failure_recovery
├── scripts/                    # ci-local.sh、备份恢复、OCR/embedding 工具、auth 密钥生成等
├── docs/                       # 实施计划与运维手册（ops/startup、failure-runbook、backup-restore）
├── knowledge_graph/            # 权威知识图谱数据（运行时只读挂载）
├── alembic.ini / *_alembic.ini / *_migrations/  # 五条独立 Alembic 迁移链
├── docker-compose.yml          # postgres + memory-api + 各 worker；frontend profile
├── docker-compose.rag.yml      # RAG 独立 PostgreSQL（55433）
└── memory-manager-execution-spec*.md  # 执行规格（施工基线）
```

## 架构概览

当前对话链路是由 LangGraph 编排的有界 Agentic RAG 工作流，而不是单次“检索后回答”。向量库本身不是图节点：`retrieve_subquery` 节点通过 `AsyncRetrieverAdapter` 调用 `RetrievalService`，再访问独立的 RAG PostgreSQL + pgvector。

```mermaid
flowchart TD
    FE["React 前端"]
    API["Conversation API<br/>事务接收 + SSE"]
    CDB[("Conversation PostgreSQL")]
    WORKER["conversation-worker<br/>claim / lease / fencing"]

    subgraph GRAPH["Conversation Agentic RAG · LangGraph"]
        START(["START：用户消息已持久化"])
        LOAD["load_conversation_context<br/>加载摘要与最近消息"]
        MEMORY["recall_memory<br/>本轮唯一一次长期记忆读取"]
        SNAPSHOT["build_turn_snapshot<br/>不可变 TurnContextSnapshot"]
        REWRITE["rewrite_and_plan<br/>独立问题 + N 个检索子问题"]
        ROUTE{"need_retrieval?"}
        EMBED["embed_subqueries<br/>批量生成查询向量"]
        FANOUT["dispatch_retrieval_workers<br/>LangGraph Send × N"]
        RETRIEVE["retrieve_subquery<br/>每个子问题独立 Worker"]
        AGG["aggregate_results<br/>确定性 Map Reducer"]
        RERANK["deduplicate_and_rerank<br/>去重、相邻块合并、证据预算"]
        EVAL["evaluate_evidence<br/>结构化充分性评估"]
        ENOUGH{"证据充分或检索预算耗尽?"}
        ANSWER["generate_answer<br/>流式结构化回答"]
        VALIDATE["validate_answer_and_citations<br/>服务端引用校验"]
        FINALIZE["persist_turn<br/>消息、事件与 Memory Outbox"]
        ACK["explicit_remember_ack<br/>显式记忆请求确认"]
        END(["END"])

        START --> LOAD --> MEMORY --> SNAPSHOT --> REWRITE --> ROUTE
        ROUTE -->|"否"| ANSWER
        ROUTE -->|"是"| EMBED --> FANOUT
        FANOUT -. "Send 子问题 1..N" .-> RETRIEVE
        RETRIEVE -. "WorkerResult Map Reducer" .-> AGG
        AGG --> RERANK --> EVAL --> ENOUGH
        ENOUGH -->|"仍缺证据且预算允许"| REWRITE
        ENOUGH -->|"充分 / 不足但预算耗尽"| ANSWER
        ANSWER --> VALIDATE --> FINALIZE
        FINALIZE -->|"普通对话"| END
        FINALIZE -->|"explicit_remember"| ACK --> END
    end

    OAI["OpenAI SDK Gateway<br/>Responses API + Structured Outputs"]
    EMBGW["QueryEmbeddingGateway<br/>OpenAI-compatible Embeddings"]
    RETGW["AsyncRetrieverAdapter<br/>受控并发 + 超时"]
    RAGSVC["RetrievalService<br/>HNSW + FTS + 公式检索 → RRF"]
    RAGDB[("独立 RAG PostgreSQL<br/>pgvector + FTS")]
    MEMAPI["Memory API / MemoryClient"]
    MEMGRAPH["MemoryManagerGraph<br/>提取、审核、合并、图谱投影"]
    MEMDB[("Memory PostgreSQL + Markdown")]
    OUTBOX["conversation-outbox-publisher"]

    FE -->|"发送消息"| API
    API -->|"原子写入消息、Turn、accepted 事件"| CDB
    CDB -->|"轮询 claim"| WORKER
    WORKER --> START
    CDB -->|"持久化 Turn Events"| API
    API -->|"SSE"| FE

    MEMORY -. "MemoryGateway" .-> MEMAPI
    REWRITE -.-> OAI
    EVAL -.-> OAI
    ANSWER -.-> OAI
    EMBED -.-> EMBGW
    RETRIEVE -.-> RETGW
    RETGW --> RAGSVC --> RAGDB
    FINALIZE -. "事务提交" .-> CDB
    CDB --> OUTBOX -->|"对话证据 / source deletion"| MEMAPI
    MEMAPI --> MEMGRAPH --> MEMDB
```

简要说明：

- **LangGraph 负责流程控制**：先读取会话与长期记忆并固化同一份快照，再由 OpenAI SDK 生成结构化检索计划；无需教材证据时直接回答，需要检索时进入多查询并行链路。
- **RAG 是节点后的检索服务**：查询先批量 Embedding，再通过 `Send × N` 并发执行 `retrieve_subquery`；底层 `RetrievalService` 将 HNSW 向量、中文 FTS 和公式检索结果用 RRF 融合。
- **证据循环有硬预算**：检索结果经过跨子问题去重、相邻块合并、确定性重排和 Token 预算裁剪；证据仍不足且预算允许时才回到 `rewrite_and_plan`，不会无限自循环。
- **回答与引用可追溯**：回答由 OpenAI SDK 单次流式生成，引用内容和 Citation ID 由服务端最终证据集生成并校验，Turn Events 持久化后通过 SSE 推送。
- **Memory 与 RAG 分域**：本轮只读取一次长期记忆；回答提交后再通过 Conversation Outbox 将对话证据可靠投递给 MemoryManagerGraph，Conversation、RAG 与 Memory 数据库彼此隔离。

关键概念：

- **MemoryManagerGraph**：内部异步记忆处理工作流（提取 / 审核 / 合并 / 图谱投影），不直接暴露给浏览器；外部通过 Gateway / MemoryClient 交互
- **领域隔离**：memory、auth、conversation、community、rag 各有独立数据库与最小权限账号
- **错误信封**：各域统一 `PublicError`（code / message / retryable / trace_id），422 区分 `REQUEST_EXTRA_FIELD` / `INVALID_PAYLOAD`
- **Feature flags 默认关闭**：community 三条链路、conversation memory_read/submit 等——"实现不等批准，启用必须等批准"

## 常用任务

### 本地全量 CI

```bash
scripts/ci-local.sh                    # 全部 stage
scripts/ci-local.sh backend-lint       # Ruff + mypy
scripts/ci-local.sh backend-unit       # 单元测试（无需数据库）
scripts/ci-local.sh backend-integration # 自动建 *_test 库并迁移（需 Docker）
scripts/ci-local.sh frontend           # 前端 lint + vitest + build
scripts/ci-local.sh contracts          # 契约测试
```

### 测试

```bash
# 后端单元测试（无需数据库）
uv run pytest tests/unit tests/test_mineru_ocr_*.py

# 集成测试（conftest 会拒绝任何非 *_test 测试库，用 ci-local 入口最省事）
scripts/ci-local.sh backend-integration

# 契约测试；路由/schema 变更后更新 OpenAPI 快照
uv run pytest tests/contract
UPDATE_OPENAPI_SNAPSHOT=1 .venv/bin/python -m pytest tests/contract -q

# 前端
cd frontend && npm run lint && npm run test && npm run build
```

### 备份与恢复

```bash
scripts/backup.sh    # age 加密备份（见 docs/ops/backup-restore.md）
scripts/restore.sh
```

## 配置

所有环境变量集中在 `backend/settings.py`，样例见 `.env.example`。常用项：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DATABASE_URL` | memory 库连接串 | `postgresql+psycopg://memory:memory@127.0.0.1:55432/memory` |
| `DEV_AUTH_ENABLED` | 开发身份模拟开关（仅 development） | `true` |
| `AUTH_ISSUER` / `AUTH_AUDIENCE` | JWT 签发/校验的 issuer 与 audience | `gewu-auth` / `memory-api` |
| `CONVERSATION_DATABASE_URL` | conversation 库 | `…@127.0.0.1:55432/conversation` |
| `COMMUNITY_DATABASE_URL` | community 库；未配置时路由不挂载、readiness 不报错 | — |
| `RAG_DATABASE_URL` | RAG 独立库 | `…@127.0.0.1:55433/rag` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | LLM 凭据与端点 | — |

生产模式（`APP_ENV=production`）下 Settings 构造强校验：必须提供 RSA2048 私钥（权限精确 0600）、匹配公钥与显式 `AUTH_DATABASE_URL`，缺配置直接抛错。本地开发可用 `scripts/generate_auth_keys.sh` 生成密钥对。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `/health/ready` 报 `knowledge_graph_registry_not_loaded` | `uv run python -m backend.memory.cli sync-knowledge-graph --apply` |
| 集成测试报"拒绝非测试库" | 必须使用 `*_test` 独立库；直接用 `scripts/ci-local.sh backend-integration` 自动处理 |
| 本地 5432 连不上 PostgreSQL | 本项目使用非默认端口 **55432**（RAG 为 55433），确认 `docker compose ps` 状态 |
| 生产模式启动即抛错 | 缺 `AUTH_PRIVATE_KEY_FILE`（RSA2048、0600）等强校验配置；见 `.env.example` 认证段注释 |
| 前端 5173 请求代理失败 | 确认后端 8000 端口在跑；检查 `frontend/.env` 的 `MEMORY_DEV_API_TARGET` / `MEMORY_DEV_USER_ID` |
| 对话功能无响应 | 确认 conversation worker/publisher 进程已启动，且 `OPENAI_*` 模型角色配置齐全 |

更多运维细节见 `docs/ops/`（startup.md / failure-runbook.md / backup-restore.md）。

## 文档索引

- **执行规格（施工基线）**：`memory-manager-execution-spec-v1.1.md`；架构原则：`memorymangergraph.md`；缺口裁决：`memory-manager-execution-spec-gap-analysis.md`
- **实施计划**：`docs/auth-service-implementation-plan.md`、`docs/conversation-agentic-rag-implementation-plan.md`、`docs/community-implementation-plan.md`、`docs/rag-phase3.md`
- **运维**：`docs/ops/startup.md`、`docs/ops/failure-runbook.md`、`docs/ops/backup-restore.md`
- **开发者/AI 约定**：`AGENTS.md`

代码内注释引用的 `规格 §X` / `方案 §X` 即指上述文档，修改行为前请先阅读对应章节。

## 开发约定

- 注释、docstring、commit message 全用简体中文；commit 风格：`feat|fix|chore(域): 中文描述`
- Ruff 行宽 100；`backend/**/api/` 忽略 B008（FastAPI Depends 工厂模式）
- `scripts/`（OCR/embedding 工具）不在 lint 门禁范围内
- Feature flags 不擅自开启

## License

私有项目，未提供开源许可证。
