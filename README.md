# xueshen-math

> English version: [README.en.md](./README.en.md)
>
> **文档基线**：`ff01255`（2026-08-16），对应 Study 学习编排域 Phase 0–4 合并及评审收口。本文只描述该提交已经落地的功能；工作区未提交内容不纳入说明。

**MemoryManagerGraph** —— 面向数学学习的长期记忆、知识图谱和学习编排系统。项目把对话、练习、社区活动和学习计划拆成相互隔离的领域：长期记忆负责沉淀可解释的学习事实，Conversation 负责 Agentic RAG 数学答疑，Study 负责把目标变成可执行计划，RAG 负责教材证据检索，Community 负责学习协作。

## 功能与当前状态

| 功能 | 已提交能力 | 默认状态 |
| --- | --- | --- |
| **MemoryManagerGraph** | 对话/活动证据异步提取、候选审核、Markdown 版本提交、纠正/删除/恢复、知识图谱用户 Overlay | 核心域 |
| **知识图谱** | 固定教材图谱 + 用户掌握状态、依据和推荐信号；状态变更可审计 | 核心域 |
| **智能对话** | LangGraph Agentic RAG、多查询并行检索、证据循环、结构化回答、Citation 校验、SSE 流式事件 | Agentic RAG 默认开启；Memory 读写按 flag 控制 |
| **Study 学习编排** | 结构化/对话式计划录入、AI 计划生成、确定性排期、任务与 Session、今日 Feed、推荐、自动 replan、Memory 回写 Outbox | **整个 Study 域默认关闭** |
| **RAG 教材检索** | 独立 pgvector 数据库、HNSW 向量检索、中文 FTS、公式检索、RRF 融合、书籍/章节/学段过滤和页码引用 | 可选独立服务 |
| **学习社区** | 讨论板块、帖子、回复、点赞、通知、Community Outbox 和 Activity Publisher | 路由按数据库配置挂载；发布与 Memory 投递默认关闭 |
| **认证** | 内嵌 JWT 签发服务、独立 Auth 数据库、生产 RSA 密钥校验、开发模式 Dev Auth | 开发环境可免登录 |
| **前端** | React/Vite 单页应用：对话、计划、知识地图、错题本、社区、记忆档案和统一通知 | 通过 Vite 代理连接 API |

> Study 后端已完成 Phase 0–4，但遵循“实现不等于批准启用”：`STUDY_DOMAIN_ENABLED`、Memory 读取、Daily Feed、自动调整、Memory 回写和通知 flag 默认均为 `false`。当前前端 `Plan` 页面仍保留 Memory learner 计划的兼容展示/引导，Study API 的真实计划编排需要在批准启用后继续接线。

## 技术栈

- **后端**：Python 3.13 · FastAPI（单进程多域）· SQLAlchemy 2 · Alembic · LangGraph · PostgreSQL 17
- **领域数据库**：memory、auth、conversation、community、study 五个本地 PostgreSQL 数据库，加上独立部署的 rag 数据库；共六条迁移链
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

# 4. 依次迁移已配置的领域数据库链，并同步知识图谱注册表
uv run alembic upgrade head                                  # memory 链
uv run alembic -c auth_alembic.ini upgrade head              # auth 链
uv run alembic -c conversation_alembic.ini upgrade head      # conversation 链
uv run alembic -c community_alembic.ini upgrade head         # community 链
uv run python -m backend.memory.cli sync-knowledge-graph --apply

# 5. Study 可选：启用前先在 .env 配置 STUDY_DATABASE_URL，再执行独立迁移
STUDY_DATABASE_URL='postgresql+psycopg://study:study@127.0.0.1:55432/study' \
  uv run alembic -c study_alembic.ini upgrade head

# 6. 启动 memory-api（唯一入口；按配置挂载 conversation/community/study 路由）
uv run uvicorn backend.app:app --port 8000

# 7. 另开终端启动后台进程
uv run python -m backend.memory.worker.main           # MemoryManagerGraph worker
uv run python -m backend.memory.worker.scheduler      # Memory 维护调度
uv run python -m backend.memory.worker.outbox_consumer
uv run python -m backend.conversation.worker.main     # Conversation Agentic RAG worker
uv run python -m backend.conversation.publisher.main  # Conversation outbox publisher

# Study 域启用后再启动；同一用户串行、不同用户可并发
uv run python -m backend.study.worker.main
uv run python -m backend.study.scheduler.main
uv run python -m backend.study.publisher.main          # 仅启用 Study Memory 回写时运行

# 8. 启动前端
cd frontend
npm install
npm run dev
```

Study 路由只有在 `STUDY_DOMAIN_ENABLED=true` 且 `STUDY_DATABASE_URL` 已配置时才挂载，前缀为 `/api/v1/study`。Study Publisher 在未配置 Memory 内部地址/token 或 `STUDY_MEMORY_WRITEBACK_ENABLED=false` 时会主动退出，这是为了保持跨域写入默认关闭，而不是启动故障。

### 验证

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5173 |
| API 文档（OpenAPI） | http://localhost:8000/docs |
| 就绪检查 | http://localhost:8000/health/ready |
| Study API（启用后） | http://localhost:8000/api/v1/study |

开发模式默认开启 Dev Auth（`DEV_AUTH_ENABLED=true`）：Vite 代理会把 `frontend/.env` 中 `MEMORY_DEV_USER_ID` 注入 `X-Dev-User-Id` 头，访客免登录即可浏览。调试 API 也可手动携带：

```bash
curl -H "X-Dev-User-Id: 00000000-0000-4000-8000-000000000001" \
  http://localhost:8000/health/ready
```

### Docker 一键启动

```bash
docker compose up --build            # postgres + memory-api(127.0.0.1:8001) + Memory/Conversation worker

docker compose --profile frontend up # 追加前端预览（127.0.0.1:4173）
```

主 Compose 当前不会自动开启 Study 域，也没有把 Study Worker/Scheduler/Publisher 作为默认服务；启用 Study 时按上面的宿主机命令启动，并使用独立 `study` 数据库。Community 数据库可以随 Compose 初始化，但 Community Publisher、Memory evidence 投递和 source deletion 仍受 feature flags 控制。

### RAG（可选）

RAG 使用完全独立的 PostgreSQL + pgvector 数据库（端口 55433），不会读取或修改 Memory 数据库：

```bash
docker compose -f docker-compose.rag.yml up -d --wait rag-postgres
RAG_DATABASE_URL='postgresql+psycopg://rag:rag@127.0.0.1:55433/rag' \
  uv run alembic -c rag_alembic.ini upgrade head
```

导入带 manifest/hash 的 chunk 与 embedding artifact：

```bash
RAG_DATABASE_URL='postgresql+psycopg://rag:rag@127.0.0.1:55433/rag' \
  uv run python scripts/rag_import.py \
  --chunk-root <chunk-artifact-root> \
  --embedding-root <embedding-artifact-root>
```

## 项目结构

```text
xueshen-math/
├── backend/
│   ├── app.py                  # FastAPI 唯一入口，按配置挂载各域 Router
│   ├── memory/                 # MemoryManagerGraph：操作队列、LangGraph、Markdown、知识图谱 Overlay
│   ├── auth_service/           # 内嵌 JWT 签发方；只在该进程持有签名私钥
│   ├── auth/                   # JWT 验签、user_id/scope 上下文和权限依赖
│   ├── conversation/           # Agentic RAG：API、LangGraph worker、SSE Turn Events、Outbox
│   ├── community/              # 社区 API、独立数据库、Activity Publisher 与内部 Reader
│   ├── study/                  # 学习编排：计划、任务、Session、Feed、Graph、Scheduler、Publisher
│   ├── rag/                    # RAG 导入/检索服务；连接独立 rag 数据库
│   ├── integrations/           # Conversation/Community 等跨域 Reader 适配
│   └── settings.py             # 全部环境变量、Feature Flags 和生产校验
├── frontend/                   # React 19 + Vite + TypeScript 单页应用
├── knowledge_graph/            # 权威教材知识图谱数据，运行时只读挂载
├── tests/                      # unit / integration / contract / conversation / community / study / rag
├── scripts/                    # CI、备份恢复、OCR、embedding、RAG 导入和认证密钥工具
├── docs/                       # 各域方案、差距分析、服务 token 和运维手册
├── alembic.ini                 # memory 迁移链
├── auth_alembic.ini            # auth 迁移链
├── conversation_alembic.ini    # conversation 迁移链
├── community_alembic.ini       # community 迁移链
├── study_alembic.ini           # study 迁移链
├── rag_alembic.ini             # rag 迁移链
├── *_migrations/               # 各域独立版本目录；RAG 使用 rag_migrations/
├── docker-compose.yml          # 本地共享 PostgreSQL、API、Memory/Conversation worker
├── docker-compose.rag.yml      # RAG 独立 PostgreSQL（55433）
└── memory-manager-execution-spec*.md  # Memory 施工规格与架构基线
```

## 总体架构：单进程多域，异步边界清晰

浏览器只访问 FastAPI 入口和公开 API；各领域在进程内共享认证/HTTP 基础设施，但数据库、迁移链、Worker 和跨域写入边界保持隔离。跨数据库写入不使用分布式事务，统一先写本域事实源，再由 Outbox 最终一致地投递。

```mermaid
flowchart LR
    FE["React 前端<br/>Chat / Plan / Map / Notebook / Community"] --> API["FastAPI 唯一入口<br/>认证、路由、错误信封、Readiness"]

    subgraph DOMAINS["业务领域"]
        MEM["MemoryManagerGraph<br/>长期记忆 + 知识图谱"]
        CHAT["Conversation<br/>Agentic RAG + SSE"]
        STUDY["Study<br/>计划、任务、Feed、Replan"]
        COMM["Community<br/>帖子、回复、通知"]
        AUTH["Auth Service<br/>JWT 签发/校验"]
    end

    API --> MEM
    API --> CHAT
    API --> STUDY
    API --> COMM
    API --> AUTH

    MEM --> MEMDB[("Memory DB<br/>+ Markdown")]
    CHAT --> CHATDB[("Conversation DB")]
    STUDY --> STUDYDB[("Study DB")]
    COMM --> COMMDB[("Community DB")]
    AUTH --> AUTHDB[("Auth DB")]

    CHAT -. "只读检索" .-> RAG["RetrievalService"]
    RAG --> RAGDB[("独立 RAG DB<br/>pgvector + FTS")]
    CHAT -. "证据/显式记忆" .-> MEM
    STUDY -. "Memory context / Outbox" .-> MEM
    COMM -. "Activity evidence / source deletion" .-> MEM

    MEM -. "LangGraph worker / scheduler / outbox" .-> MEMDB
    CHAT -. "worker / publisher" .-> CHATDB
    STUDY -. "worker / scheduler / publisher" .-> STUDYDB
    COMM -. "lifespan publisher" .-> COMMDB
```

### 架构原则

- **领域事实源分离**：计划、任务、Session 和 Daily Feed 只以 Study DB 为准；Conversation 只保存线程/消息/Turn Events；RAG 只保存教材 corpus；Memory 只保存长期记忆及图谱用户状态。
- **模型不直接拥有副作用权限**：OpenAI 只返回结构化理解、计划蓝图或内容表达；日期、预算、前置关系、状态迁移、版本冲突和进度由确定性代码完成。
- **异步操作可恢复**：需要较长时间或跨域投递的写入先落本域 operation/outbox，再由带 lease、fencing、幂等键和 checkpoint 的后台角色执行。
- **读接口无隐藏副作用**：尤其是 Study `GET /home`，只读已持久化结果；Daily Feed 由 `ensure-today` 或 Scheduler 创建。
- **实现不等于启用**：Community 的 Memory 投递、Conversation 的 Memory 读写、Study 的全部 flag 默认关闭，不能因为代码已提交就自行打开。

## 功能架构详解

### 1. MemoryManagerGraph：长期记忆与知识图谱

MemoryManagerGraph 是内部异步工作流，不直接暴露给浏览器，也不直接操作文件系统。所有外部域通过 Memory Gateway/MemoryClient 进入 operation 队列；所有持久化写入经过 MemoryService。

```mermaid
flowchart LR
    EVIDENCE["Conversation / Community / Study<br/>学习证据"] --> GATEWAY["Memory Gateway<br/>鉴权、Schema、幂等、user_id"]
    COMMAND["用户命令<br/>纠正 / 删除 / 恢复 / 图谱状态"] --> GATEWAY
    GATEWAY --> OPS[("memory_operations<br/>PostgreSQL")]
    OPS --> WORKER["Memory Worker<br/>claim / lease / checkpoint"]

    subgraph GRAPH["MemoryManagerGraph"]
        NORMALIZE["normalize_input<br/>校验不变量"] --> ROUTE{"route"}
        ROUTE --> SUMMARY["SummaryMemoryGraph<br/>证据提取、长期价值、MutationPlanDraft"]
        ROUTE --> KG["KnowledgeGraphStateGraph<br/>确定性掌握状态/推荐"]
        ROUTE --> CMD["Deterministic Commands<br/>纠正、删除、恢复"]
        ROUTE --> MAINT["Maintenance<br/>重建/清理/恢复"]
    end

    WORKER --> NORMALIZE
    SUMMARY --> SERVICE["MemoryService<br/>唯一持久化写入口"]
    KG --> SERVICE
    CMD --> SERVICE
    MAINT --> SERVICE
    SERVICE --> MD["Markdown 存储<br/>不可变版本 + 原子活动版本"]
    SERVICE --> INDEX[("Memory PostgreSQL<br/>索引、commit、audit、outbox")]
    SERVICE --> OUTBOX["memory_outbox<br/>通知/跨域事件"]

    CLIENT["MemoryClient / LearningContextService"] --> READ["只读上下文组装"]
    READ --> MD
    READ --> INDEX
    READ --> KGREAD["固定知识图谱<br/>+ 用户 Overlay"]
```

**关键边界：**

- SummaryMemoryGraph 可以调用 OpenAI 做结构化候选提取，但模型只产生经过 Schema 校验的 `MutationPlanDraft`；稳定 ID、`expected_version`、文件路径和最终提交由应用层补齐。
- KnowledgeGraphStateGraph 负责验证固定图谱中的 `node_id`、更新用户 Overlay、计算推荐信号和写审计，不依赖模型决定掌握状态的数据库迁移。
- Markdown 是长期记忆正文的可审计版本源，PostgreSQL 保存索引、操作、提交、审计和 Outbox；删除/恢复通过版本协议和 tombstone 保护。
- `MemoryClient` 是对话、Study、Community 等域的唯一访问边界，外部业务不应直接调用 Graph 节点、数据库或 Markdown 文件。

### 2. Conversation：Agentic RAG 数学对话

Conversation 不是“检索一次再回答”的单次链路，而是一个带硬预算、可恢复的 LangGraph 工作流。API 负责原子接收消息和 SSE，Worker 负责实际编排，Publisher 负责把对话证据可靠投递给 Memory。

```mermaid
flowchart TD
    FE["React Chat"] --> API["Conversation API<br/>事务接收 + SSE"]
    API --> CDB[("Conversation DB<br/>threads / messages / turns / events")]
    CDB --> CLAIM["conversation-worker<br/>claim / lease / fencing"]

    subgraph GRAPH["Conversation Agentic RAG · LangGraph"]
        START(["START：消息已持久化"]) --> CONTEXT["加载摘要与最近消息"]
        CONTEXT --> MEMORY["可选读取 Memory context<br/>本轮最多一次"]
        MEMORY --> SNAPSHOT["固化 TurnContextSnapshot"]
        SNAPSHOT --> PLAN["rewrite_and_plan<br/>独立问题 + 检索子问题"]
        PLAN --> NEED{"需要教材证据？"}
        NEED -->|"否"| ANSWER["generate_answer<br/>结构化流式回答"]
        NEED -->|"是"| EMBED["批量 Query Embedding"]
        EMBED --> FANOUT["LangGraph Send × N"]
        FANOUT --> RETRIEVE["每个子问题独立 retrieve_subquery"]
        RETRIEVE --> AGG["Map Reducer 聚合"]
        AGG --> RERANK["去重、相邻块合并、确定性重排"]
        RERANK --> EVAL["evaluate_evidence<br/>充分性 + 预算"]
        EVAL --> LOOP{"证据不足且预算允许？"}
        LOOP -->|"是"| PLAN
        LOOP -->|"否"| ANSWER
        ANSWER --> CITATION["服务端生成并校验 Citation"]
        CITATION --> FINALIZE["持久化 Turn Events + Memory Outbox"]
    end

    CLAIM --> START
    RETRIEVE -. "AsyncRetrieverAdapter" .-> RETRIEVER["RetrievalService"]
    RETRIEVER --> RAGDB[("独立 RAG DB<br/>HNSW + 中文 FTS + 公式")]
    PLAN -. "结构化 RewritePlan" .-> LLM["OpenAI SDK Gateway"]
    EVAL -. "结构化 EvidenceAssessment" .-> LLM
    ANSWER -. "Structured Outputs" .-> LLM
    FINALIZE --> CDB
    CDB -->|"Turn Events"| API
    API -->|"SSE"| FE
    CDB --> OUTBOX["conversation-outbox-publisher"]
    OUTBOX -. "对话证据 / source deletion" .-> MEM["Memory API / MemoryManagerGraph"]
```

**关键边界：**

- `rewrite_and_plan` 先拆分独立问题，再批量生成向量；`Send × N` 并行检索，最后由确定性 reducer、RRF/重排和 token 预算裁剪形成最终证据集。
- 证据不足才允许回到重写节点，循环次数受检索预算限制；回答只由最终证据集生成，Citation ID 由服务端生成并校验，避免模型伪造引用。
- Conversation 只在本轮需要时读取一次长期记忆；回答提交后的证据通过 Outbox 投递给 Memory，Conversation、RAG 和 Memory 数据库不跨库直写。
- SSE 推送的是已经持久化的 Turn Events，不把模型流式输出作为唯一事实源；Worker 重启可依靠 operation/lease/checkpoint 恢复。

### 3. Study：学习计划与主动学习编排

Study 是最新提交新增的独立后端域。它把目标、截止日期、每周可学习日和每日分钟预算转成计划、revision、任务、Session、今日 Feed 和近 7 天统计。Study DB 是这些执行数据的唯一事实源，Memory 只提供长期上下文和接收异步回写。

```mermaid
flowchart LR
    PLAN_UI["计划页 / 对话式录入"] --> API["Study API<br/>/api/v1/study"]
    HOME_UI["今日主页"] --> API
    API --> DB[("Study DB<br/>plans / revisions / tasks / sessions / feed / stats")]

    API -. "同步单轮 intake" .-> INTAKE["Plan Intake Graph<br/>抽取 → 校验 → 追问 → 预览"]
    INTAKE -. "结构化模型调用" .-> LLM["OpenAI SDK<br/>仅负责结构化输出"]
    API --> OPS[("Study Operations<br/>幂等 + lease")]
    ENSURE["POST /home/ensure-today<br/>显式创建 Feed operation"] --> OPS
    SCHED["Study Scheduler<br/>用户 IANA 时区 / 300s 扫描"] --> OPS

    OPS --> WORKER["Study Worker<br/>同用户串行、跨用户并发"]
    subgraph GRAPHS["Study LangGraph 异步图"]
        PLAN["Plan Generation Graph<br/>Memory context → blueprint"]
        FEED["Daily Feed Graph<br/>正式任务 + 最多两条推荐"]
        REPLAN["Replan Graph<br/>顺延 / 周期复盘 / revision"]
    end
    WORKER --> PLAN
    WORKER --> FEED
    WORKER --> REPLAN

    PLAN --> RULES["确定性排期引擎<br/>日期、时区、预算、前置、冲突、进度"]
    FEED --> RULES
    REPLAN --> RULES
    RULES --> DB
    LLM -. "目标理解 / 任务文案 / 推荐理由" .-> PLAN
    LLM -. "Feed 文案" .-> FEED

    PLAN -. "可选读取" .-> MEMREAD["Memory Gateway<br/>learner / mastery / graph"]
    FEED -. "可选读取" .-> MEMREAD
    REPLAN -. "可选读取" .-> MEMREAD
    DB --> OUTBOX["Study Outbox Publisher"]
    OUTBOX -. "计划摘要 / 活动证据" .-> MEMWRITE["Memory API<br/>异步回写"]
```

**关键边界：**

- Intake 每轮在 API 请求内同步执行轻量结构化抽取和追问；用户 `confirm` 后才创建计划生成 operation。计划在确认前不会激活，模型回复文本也不能直接成为正式计划。
- Plan Generation 由 OpenAI 生成受允许知识点约束的任务蓝图；确定性代码负责最终日期、时区/DST、每日时间预算、前置关系、休息日、任务拆分、冲突检测和进度口径。
- Daily Feed 不放在 `GET /home` 的副作用链路里：前端看到 `generation_status=pending` 后调用 `ensure-today`，Scheduler 再以 `(user_id, plan_id, local_date)` 幂等键兜底。正式任务与自适应推荐分离，只有用户接受推荐后才创建正式任务。
- Replan 只修改未来、未完成且未锁定的任务；命中重大调整阈值时生成 `proposed revision`，用户通过 accept/reject 和 `expected_version` 完成 CAS 决策，不能由模型自行判断“显著变化”。
- 手动操作是第一版唯一正式完成来源；Session heartbeat 只记录真实学习活跃分钟，不能用任务完成接口伪造学习时长。Study 的 Memory 读、Daily Feed、自动调整、Memory 回写和通知均默认关闭。

### 4. RAG：教材导入与证据检索

RAG 是独立的数据与检索域。它不把 embedding、chunk 或教材全文写进 Memory 数据库；Conversation 只通过 `RetrievalService` 读取已激活的 corpus。

```mermaid
flowchart LR
    ARTIFACT["OCR / chunk / embedding artifacts<br/>manifest + SHA-256"] --> IMPORT["rag_import.py<br/>校验关联、维度、数量、hash"]
    IMPORT --> VERSION["不可变 corpus_version<br/>loading → ready → active"]
    VERSION --> RAGDB[("RAG PostgreSQL 55433<br/>pgvector + FTS + formula_terms")]

    QUERY["Conversation 子问题"] --> EMBED["QueryEmbeddingGateway"]
    EMBED --> RETRIEVAL["RetrievalService"]
    RETRIEVAL --> VECTOR["HNSW cosine<br/>向量 Top-K"]
    RETRIEVAL --> FTS["中文 simple FTS<br/>术语/二元组"]
    RETRIEVAL --> FORMULA["公式规范化 + GIN<br/>精确匹配"]
    VECTOR --> RRF["RRF 融合 + retrieval_weight<br/>过滤与引用裁剪"]
    FTS --> RRF
    FORMULA --> RRF
    RRF --> EVIDENCE["Evidence blocks<br/>书籍 / 章节 / 页码 / source_refs"]
    RAGDB --> RETRIEVAL
    EVIDENCE --> QUERY
```

导入 run 会记录状态、数量、artifact hash 和错误详情；同一 `(chunk_build_id, embedding_profile_id)` 幂等。检索同时支持 HNSW、中文 FTS 和公式精确匹配，融合后再执行书籍、学段、章节、内容类型和页码范围过滤。重新导入失败只影响新 corpus，不覆盖旧的 active corpus。

### 5. Community：学习社区与活动证据

Community 使用独立数据库保存公共高频读写内容，不跨库创建 Auth 外键，只保留 `user_id`。Publisher 在 FastAPI lifespan 中运行，不新增端口；它把社区活动转成受控的 Activity Evidence 或 source deletion 请求。

```mermaid
flowchart LR
    UI["Community UI"] --> API["Community API<br/>boards / posts / replies / likes / notifications"]
    API --> AUTH["Auth Context<br/>user_id + scope"]
    API --> DB[("Community DB<br/>posts / replies / outbox / notifications")]
    DB --> PUB["ActivityPublisher<br/>claim + lease + retry + dead-letter"]
    PUB -. "COMMUNITY_MEMORY_SUBMIT_ENABLED" .-> READER["Community Source Reader"]
    READER -. "ActivityEvidence" .-> MEM["Memory API / MemoryManagerGraph"]
    PUB -. "COMMUNITY_SOURCE_DELETION_ENABLED" .-> DELETE["Memory source deletion"]
```

帖子、回复、点赞和通知在 Community DB 内完成事务；跨域投递不参与社区写事务，依靠 Outbox 最终一致。Publisher 会按 feature flag、来源状态、HTTP 错误分类和 lease/fencing 规则处理重试与 dead-letter。`COMMUNITY_PUBLISHER_ENABLED`、`COMMUNITY_MEMORY_SUBMIT_ENABLED` 和 `COMMUNITY_SOURCE_DELETION_ENABLED` 默认关闭。

### 6. Auth：统一身份与领域授权

Auth Service 与 memory-api 同进程，负责签发 JWT；验签和权限依赖位于 `backend/auth/`。领域 API 只使用认证上下文中的 `user_id`，不信任客户端请求体自行传入的用户身份。

```mermaid
flowchart LR
    USER["浏览器 / 客户端"] --> LOGIN["/api/v1/auth<br/>register / login / refresh"]
    LOGIN --> SERVICE["内嵌 Auth Service<br/>签发 JWT"]
    SERVICE --> AUTHDB[("Auth DB<br/>用户、refresh token、事件")]
    KEYS["RSA 私钥/公钥<br/>生产严格校验"] --> SERVICE
    SERVICE --> TOKEN["JWT access token"]
    TOKEN --> REQUEST["Memory / Conversation / Study / Community 请求"]
    REQUEST --> VERIFY["Auth verifier<br/>issuer / audience / scope"]
    VERIFY --> ROUTES["领域 Router<br/>user_id 隔离 + 权限检查"]
    DEV["开发模式 X-Dev-User-Id"] -. "仅 development" .-> VERIFY
```

生产环境要求 RSA-2048 私钥权限精确为 `0600`、匹配公钥和显式 `AUTH_DATABASE_URL`；开发环境可以用 `DEV_AUTH_ENABLED=true` 和 `X-Dev-User-Id` 进行本地体验。内部跨域 Publisher 使用单独的 service token，不把用户 JWT 当作后台投递凭据。

## 领域与数据隔离

| 领域 | 唯一事实源 | 运行时角色 | 跨域关系 |
| --- | --- | --- | --- |
| Memory | Memory PostgreSQL + Markdown | API、Memory Worker、Scheduler、Outbox Consumer | 接收 Conversation/Community/Study 证据；提供 Memory context |
| Auth | Auth PostgreSQL | 内嵌签发服务 + 验签依赖 | 只提供身份、token 和 scope，不被业务表跨库外键引用 |
| Conversation | Conversation PostgreSQL | API、Agentic RAG Worker、Outbox Publisher | 读取 RAG；可选读取/投递 Memory |
| Community | Community PostgreSQL | API + FastAPI lifespan Activity Publisher | 通过 Outbox 投递 Activity Evidence/source deletion |
| Study | Study PostgreSQL | API、Study Worker、Scheduler、Outbox Publisher | 通过 Memory Gateway 读长期上下文、异步回写计划/活动 |
| RAG | 独立 RAG PostgreSQL（55433） | 导入 CLI + RetrievalService | 只向 Conversation 提供教材证据，不读取 Memory |

所有领域统一使用 `PublicError` 信封（`code` / `message` / `retryable` / `trace_id`）；写接口使用幂等键和版本/CAS 保护。跨数据库写入不做分布式事务，按“本域提交 → Outbox → 目标域幂等消费”执行。

## 常用任务

### 本地全量 CI

```bash
scripts/ci-local.sh                     # 全部 stage
scripts/ci-local.sh backend-lint        # Ruff + mypy
scripts/ci-local.sh backend-unit        # 单元测试（无需数据库）
scripts/ci-local.sh backend-integration # 自动建 memory/auth/conversation/community/study_test 并迁移
scripts/ci-local.sh frontend            # 前端 lint + vitest + build
scripts/ci-local.sh contracts           # 契约测试
```

### 测试

```bash
# 后端单元测试（无需数据库）
uv run pytest tests/unit tests/test_mineru_ocr_*.py

# Study 专项单元/集成测试（集成测试需要 study_test，推荐统一入口）
uv run pytest tests/unit/test_study_*.py
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

## 配置与 Feature Flags

所有环境变量集中在 `backend/settings.py`，完整样例见 `.env.example`。常用配置如下：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `DATABASE_URL` | memory 库连接串 | `postgresql+psycopg://memory:memory@127.0.0.1:55432/memory` |
| `AUTH_DATABASE_URL` | auth 库连接串 | `…@127.0.0.1:55432/auth` |
| `CONVERSATION_DATABASE_URL` | conversation 库连接串 | `…@127.0.0.1:55432/conversation` |
| `COMMUNITY_DATABASE_URL` | community 库；未配置时不挂载社区路由 | — |
| `STUDY_DATABASE_URL` | study 库；域开关关闭或未配置时不挂载 Study 路由 | — |
| `RAG_DATABASE_URL` | RAG 独立库 | `…@127.0.0.1:55433/rag` |
| `DEV_AUTH_ENABLED` | 开发身份模拟开关（仅 development） | `true` |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` | LLM 凭据与端点 | — |

默认关闭或需要批准的关键 flag：

```text
# Study
STUDY_DOMAIN_ENABLED=false
STUDY_MEMORY_READ_ENABLED=false
STUDY_DAILY_FEED_ENABLED=false
STUDY_AUTO_REPLAN_ENABLED=false
STUDY_MEMORY_WRITEBACK_ENABLED=false
STUDY_NOTIFICATION_ENABLED=false

# Conversation 的跨域 Memory transport
CONVERSATION_MEMORY_READ_ENABLED=false
CONVERSATION_MEMORY_SUBMIT_ENABLED=false

# Community 的 Publisher / Memory evidence / source deletion
COMMUNITY_PUBLISHER_ENABLED=false
COMMUNITY_MEMORY_SUBMIT_ENABLED=false
COMMUNITY_SOURCE_DELETION_ENABLED=false
```

`CONVERSATION_AGENTIC_RAG_ENABLED`、`CONVERSATION_MULTI_QUERY_ENABLED`、`CONVERSATION_EVIDENCE_LOOP_ENABLED` 和 `CONVERSATION_STREAMING_ENABLED` 默认开启；这不等于允许它读取或写入 Memory。

生产模式（`APP_ENV=production`）下 Settings 构造强校验：必须提供 RSA2048 私钥（权限精确 0600）、匹配公钥与显式 `AUTH_DATABASE_URL`，缺配置直接抛错。本地开发可用 `scripts/generate_auth_keys.sh` 生成密钥对。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `/health/ready` 报 `knowledge_graph_registry_not_loaded` | `uv run python -m backend.memory.cli sync-knowledge-graph --apply` |
| `/health/ready` 报 `study_database_not_configured` | 检查 `STUDY_DOMAIN_ENABLED` 与 `STUDY_DATABASE_URL`；不启用 Study 时保持 domain flag 为 `false` |
| Study 路由没有出现在 OpenAPI | Study 只有在 domain flag 和数据库 URL 同时满足时才挂载；检查 `.env` 后重启 API |
| Study Worker 启动后没有生成 Feed | 确认 `STUDY_DAILY_FEED_ENABLED=true`、Study Scheduler/Worker 已启动，并从 `GET /home` 的 `generation_status=pending` 触发 `POST /home/ensure-today` |
| 集成测试报“拒绝非测试库” | 必须使用 `*_test` 独立库；直接用 `scripts/ci-local.sh backend-integration` 自动处理 |
| 本地 5432 连不上 PostgreSQL | 本项目使用非默认端口 **55432**（RAG 为 55433），确认 `docker compose ps` 状态 |
| 生产模式启动即抛错 | 缺 `AUTH_PRIVATE_KEY_FILE`（RSA2048、0600）等强校验配置；见 `.env.example` 认证段注释 |
| 前端 5173 请求代理失败 | 确认后端 8000 端口在跑；检查 `frontend/.env` 的 `MEMORY_DEV_API_TARGET` / `MEMORY_DEV_USER_ID` |
| 对话功能无响应 | 确认 conversation worker/publisher 进程已启动，且 `OPENAI_*` 模型角色配置齐全；需要教材证据时同时检查 RAG 数据库 |
| Study Publisher 直接退出 | 这是默认 fail-closed 行为：配置 `MEMORY_API_BASE_URL`、`MEMORY_AGENT_TOKEN` 并显式打开 `STUDY_MEMORY_WRITEBACK_ENABLED` 后再启动 |

更多运维细节见 `docs/ops/`（startup.md / failure-runbook.md / backup-restore.md）。

## 文档索引

- **Memory 架构与施工规格**：[`memorymangergraph.md`](./memorymangergraph.md)、[`memory-manager-execution-spec-v1.1.md`](./memory-manager-execution-spec-v1.1.md)、[`memory-manager-execution-spec-gap-analysis.md`](./memory-manager-execution-spec-gap-analysis.md)
- **Conversation / Agentic RAG**：[`docs/conversation-decision-items.md`](./docs/conversation-decision-items.md)、[`docs/conversation-gap-analysis.md`](./docs/conversation-gap-analysis.md)
- **Study 学习编排**：[`docs/study-plan-push-implementation-plan.md`](./docs/study-plan-push-implementation-plan.md)
- **Community 社区**：[`docs/community-implementation-plan.md`](./docs/community-implementation-plan.md)、[`docs/community-service-tokens.md`](./docs/community-service-tokens.md)
- **RAG**：[`docs/rag-phase3.md`](./docs/rag-phase3.md)
- **运维**：[`docs/ops/startup.md`](./docs/ops/startup.md)、[`docs/ops/failure-runbook.md`](./docs/ops/failure-runbook.md)、[`docs/ops/backup-restore.md`](./docs/ops/backup-restore.md)
- **开发者/AI 约定**：[`AGENTS.md`](./AGENTS.md)

代码内注释引用的 `规格 §X` / `方案 §X` 即指上述文档，修改行为前请先阅读对应章节。

## 开发约定

- 注释、docstring、commit message 全用简体中文；commit 风格：`feat|fix|chore(域): 中文描述`
- Ruff 行宽 100；`backend/**/api/` 忽略 B008（FastAPI Depends 工厂模式）
- `scripts/`（OCR/embedding 工具）不在 lint 门禁范围内
- Feature flags 不擅自开启

## License

私有项目，未提供开源许可证。
