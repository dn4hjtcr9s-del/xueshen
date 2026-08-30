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


---

## 生产部署（community-rebuild-plan.md Phase 5，v3.9 冻结）

部署目录 `/opt/xueshen`（git clone/pull）；自研镜像服务器本地 `docker compose -f docker-compose.prod.yml build`，tag = git short SHA（`GIT_SHA`）。公网入口只有宿主机 nginx（443），memory-api 仅绑定 `127.0.0.1:8000`。

**与计划 5.1/5.3/5.4 的偏离（所有者签字确认后生效）**：计划原设计 compose 内含 nginx + certbot 容器；实际宿主机已装 nginx 1.24 + certbot（`xueshen.xin` ECDSA 证书，certbot.timer 自动续期），改为宿主机 nginx 反代 + 托管 `frontend/dist`，compose 不含 nginx/certbot。站点配置 `deploy/nginx/xueshen.conf`。

### 启动顺序（5.2 冻结，命令级）

```bash
cd /opt/xueshen
export GIT_SHA=$(git rev-parse --short HEAD)
# ① 建密钥（首次）：mkdir -p secrets && bash scripts/generate_auth_keys.sh（产物移入 secrets/ 并 chmod 600）
# ② 前端构建（一次性 job）
docker compose -f docker-compose.prod.yml run --rm frontend-build
# ③ postgres
docker compose -f docker-compose.prod.yml up -d postgres
# ④ 五链迁移（restart:"no"，任一链失败即整体失败、应用不启动）
docker compose -f docker-compose.prod.yml run --rm migrate
# ⑤ 应用 + workers + backup
docker compose -f docker-compose.prod.yml up -d
# ⑥ 宿主机 nginx（首次安装站点配置后）：nginx -t && systemctl reload nginx
# ⑦ 冒烟：curl http://127.0.0.1:8000/health/ready；浏览器走 5.7 清单
```

### 备份与恢复（5.5）

backup 容器每日 03:17（UTC）逐库 `pg_dump -Fc` 到 named volume `backups`；任一失败产生 `/backups/FAILED-{yyyymmdd}` 标记（巡检发现，MVP 无主动告警）；成功则清 7 天前旧备份。恢复演练：`pg_restore` 到新建临时库，恢复后按需 `alembic upgrade`。

### 5.6 部署记录表（格式冻结；任一行未确认 → 禁止进入 5.7 真实部署）

| 项 | 值 | 确认人 | 确认日期 |
|---|---|---|---|
| postgres tag | 待抄录 | | |
| nginx（宿主机）版本 | 1.24.0 (Ubuntu) | | |
| certbot（宿主机）版本/续期 | certbot.timer 自动续期 | | |
| alpine tag + postgresql17-client apk 版本 | 待抄录 | | |
| python 基础镜像补丁版（根 Dockerfile FROM） | 待抄录（当前 `python:3.13-slim`，P5 改补丁版后独立 chore 提交） | | |
| node 构建镜像补丁版 | 待抄录 | | |
| Docker Engine / Compose 版本 | 29.7.2 / v5.5.0 | | |
| git commit | 待填（部署时 HEAD） | | |
| 回滚用旧 commit | 待填 | | |
| 前端产物目录 | /opt/xueshen/frontend/dist | | |
| 域名 / CDN 域名 / DNS | xueshen.xin + www / 待填 / A 记录已生效 | | |
| Kodo 五项（脱敏：只记是否已配置） | 待确认 | | |
| 管理员 UUID 名单（脱敏） | 待确认 | | |
| 升配完成（2C4G + 2G swap） | 已完成（3.4Gi + 2Gi swap） | | |
| 偏离项：宿主机 nginx/certbot 替代容器化 | 待所有者签字 | | |

### 环境变量

模板 `deploy/env.production.example` → `/opt/xueshen/.env.production`（chmod 600，`.env.*` 已 gitignore）。生产启动强校验：Kodo 五项 + `COMMUNITY_ADMIN_USER_IDS` + 认证密钥，缺即 Settings 构造抛错、进程不启动。

### 回滚（5.6 冻结）

先 downgrade（`alembic -c community_alembic.ini downgrade`，0002 downgrade 会清除附件/申请数据，已接受）→ 再切回旧镜像（`GIT_SHA=<旧>` 重新 `up -d`）。
