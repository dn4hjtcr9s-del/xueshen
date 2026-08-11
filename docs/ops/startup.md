# 启动手册（本地）

## 进程清单

| 进程 | 启动命令 | 职责 |
| --- | --- | --- |
| postgres | `docker compose up -d postgres` | PostgreSQL 17（含 pg_trgm） |
| memory-api | `uv run uvicorn backend.app:app --host 0.0.0.0 --port 8000` | Gateway API（`backend.app:app`） |
| memory-worker | `uv run python -m backend.memory.worker.main` | operation 执行（LangGraph） |
| memory-scheduler | `uv run python -m backend.memory.worker.scheduler` | 定时维护任务、Lease 回收、备份检查告警 |
| memory-outbox-consumer | `uv run python -m backend.memory.worker.outbox_consumer` | Outbox 投递（通知/投影/事件日志） |
| frontend | `cd frontend && npm run dev` | Vite 开发服务器（5173） |

整套栈也可以用 `docker compose up -d` 一次拉起（compose 内含全部六个服务）。

## 首次初始化（按顺序）

1. `docker compose up -d postgres`
2. `uv run alembic upgrade head` — 建全部表
3. `uv run python -m backend.memory.cli sync-knowledge-graph --apply` — 加载知识图谱注册表（`knowledge_graph_nodes/edges`），不加载则 `/health/ready` 报 `knowledge_graph_registry_not_loaded`
4. 启动 memory-api / memory-worker / memory-scheduler / memory-outbox-consumer
5. `cd frontend && npm install && npm run dev`

## 日常启动

`docker compose up -d postgres` 后启动四个后端进程即可；迁移有新版本时先 `uv run alembic upgrade head`。

## 健康检查（§14.8）

```text
GET /health/live     进程存活，不访问外部依赖
GET /health/startup  启动初始化完成
GET /health/ready    PostgreSQL、迁移版本、存储目录可写、图谱注册表已加载
GET /metrics         Prometheus 指标（不含 user_id）
```

`/health/ready` 的 `failures` 取值与排查见 failure-runbook.md。

## 关键环境变量

- `DATABASE_URL`、`MEMORY_STORAGE_ROOT`（默认 `.local/memory`）
- 开发认证：`DEV_AUTH_ENABLED=true`（生产必须为 false）
- 备份：`BACKUP_ROOT`（默认 `.local/backups`）、`BACKUP_AGE_RECIPIENT`、`BACKUP_AGE_IDENTITY_FILE`
- 日志：`LOG_LEVEL`、`LOG_HMAC_KEY`（日志中 user_id 只以 HMAC 摘要出现）

`.local/` 已加入 `.gitignore`；密钥与备份不得入库。
