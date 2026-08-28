# AGENTS.md

MemoryManagerGraph（xueshen-math）：数学教材长期记忆 + 知识图谱 + 对话/社区服务。FastAPI 单进程多域后端 + React(Vite) 前端，uv 管理，Python 3.13（`[tool.uv] package = false`，快速上手见 `README.md` / `README.en.md`，设计文档在根目录 spec md 与 `docs/`）。

## 参考项目

OPUS-5.md和codex-rs文件夹是参考模仿的项目，你不能更改。默认不去读它，除非用户要求。

## 命令

- 环境：`uv sync --extra dev --extra ocr`（dev 含 pytest/ruff/mypy；ocr 含 pypdf）
- 本地 CI 统一入口：`scripts/ci-local.sh [stage ...]`，stage = backend-lint / backend-unit / backend-integration / frontend / contracts / container-build
- 代码门禁（backend-lint）：`uv run ruff check backend tests && uv run ruff format --check backend tests && uv run mypy backend`。注意：`scripts/`（OCR/embedding 工具）**不在**门禁范围，存量 40+ 错误不要顺手"修"
- 单元测试（无需数据库）：`uv run pytest tests/unit tests/test_mineru_ocr_*.py`
- 集成测试：必须先起 `docker compose up -d --wait postgres`，且**必须**用 `*_test` 独立库（memory_test/auth_test/conversation_test/community_test）——conftest 会拒绝任何非测试库。标准做法 `scripts/ci-local.sh backend-integration`（自动建库、各链迁移、同步图谱），覆盖 tests/integration + tests/failure_recovery + tests/conversation + tests/community
- 契约测试：`uv run pytest tests/contract`；路由/schema 变更后须 `UPDATE_OPENAPI_SNAPSHOT=1 .venv/bin/python -m pytest tests/contract -q` 更新快照并在 review 中确认
- RAG 测试（tests/rag）默认纯单元无需数据库；test_rag_integration.py 仅在设置了 `RAG_TEST_DATABASE_URL` 时运行
- 前端：`cd frontend && npm run dev`（5173）；`npm run lint` / `npm run test`(vitest) / `npm run build`(tsc --noEmit && vite build)

## 数据库与迁移

- 本地 PostgreSQL 端口 **55432**（非默认 5432）；RAG 独立库在 **55433**（`docker-compose.rag.yml`）
- 六条独立 Alembic 链，各有 ini：`alembic.ini`(memory)、`auth_alembic.ini`、`conversation_alembic.ini`、`community_alembic.ini`、`study_alembic.ini`，另 `rag_alembic.ini` + `rag_migrations/`。默认 `uv run alembic upgrade head` 只迁 memory 链；Study 链迁移：`STUDY_DATABASE_URL=...study uv run alembic -c study_alembic.ini upgrade head`
- 首次初始化顺序：postgres → 各链 upgrade head → `uv run python -m backend.memory.cli sync-knowledge-graph --apply`（否则 /health/ready 报 knowledge_graph_registry_not_loaded）→ 启动各进程

## 启动（本地开发）

- memory-api：`uv run uvicorn backend.app:app --port 8000`；backend/app.py 是唯一入口，按配置条件挂载 conversation/community 路由
- 后台进程：`uv run python -m backend.memory.worker.main` / `.scheduler` / `.outbox_consumer`；Conversation：`backend.conversation.worker.main` / `backend.conversation.publisher.main`；Study（STUDY_DOMAIN_ENABLED=true 时）：`backend.study.worker.main` / `backend.study.scheduler.main` / `backend.study.publisher.main`
- 开发认证默认开启（DEV_AUTH_ENABLED=true），用 `X-Dev-User-Id` 头模拟身份；生产下 Settings 构造强校验（RSA2048 私钥、0600 权限等），缺配置直接抛错
- 运维细节：docs/ops/startup.md、failure-runbook.md、backup-restore.md

## 架构

- `backend/memory/`：核心长期记忆域。API（api/）→ operation 队列 → LangGraph worker（graph/）→ 事务化提交 + outbox（persistence/）；正文存 Markdown（storage/），PostgreSQL 存索引/事件/图谱状态
- `backend/auth_service/`：内嵌 JWT 签发方，与 memory-api 同进程（app.py 启动时 build_auth_runtime）；`backend/auth/` 是验签方
- `backend/conversation/`：Agentic RAG 对话域，独立 conversation 库 + 独立 worker/publisher，SSE 流式
- `backend/community/`：社区域，独立 community 库；未配置 COMMUNITY_DATABASE_URL 时不挂载路由、readiness 不报错
- `backend/rag/`：RAG 导入/检索库（独立 rag 库），被 conversation 检索复用
- `backend/study/`：学习编排域（docs/study-plan-push-implementation-plan.md v1.2），独立 study 库；未启用 STUDY_DOMAIN_ENABLED 时不挂载路由、readiness 不报错
- 错误：各域 `contracts/errors.py` 定义域错误，统一 PublicError 信封（code/message/retryable/trace_id）；422 区分 REQUEST_EXTRA_FIELD / INVALID_PAYLOAD

## 约定

- 注释/docstring/commit message 全用简体中文（commit 风格 `feat|fix|chore(域): 中文描述`）
- 设计文档：根目录 `memory-manager-execution-spec*.md`、`docs/community-implementation-plan.md`、`docs/rag-phase3.md`、`docs/superpowers/`。代码内 `规格 §X` / `方案 §X` 即引用这些文档，改行为前先读对应章节
- Ruff：行宽 100，select E,F,I,UP,B,ASYNC,RUF，忽略 RUF001-003（中文全角标点有意为之）；api/ 目录忽略 B008（Depends 工厂模式）。mypy strict 只作用于 backend/
- Feature flags 默认关闭（community 三条链路、conversation memory_read/submit 等）："实现不等批准，启用必须等批准"，不要擅自开启
- 环境变量集中在 backend/settings.py，参考 `.env.example`；`.env` 与 `.local/` 不入库
- 集成测试 conftest 每测试 TRUNCATE 用户表、保留 knowledge_graph 注册表；测试库由 admin 角色创建、归属各自最小权限角色
