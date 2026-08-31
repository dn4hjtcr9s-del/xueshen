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
# ⓪ 首次：cp deploy/env.production.example .env.production 填值 && chmod 600 .env.production
#    && ln -sf .env.production .env（compose 插值只读 .env；服务 env_file 读 .env.production）
# ① 建密钥（首次）：bash scripts/generate_auth_keys.sh secrets（0600 已在脚本内保证）
# ② 前端构建（一次性 job）
docker compose -f docker-compose.prod.yml run --rm frontend-build
# ③ postgres
docker compose -f docker-compose.prod.yml up -d postgres
# ④ 五链迁移（restart:"no"，任一链失败即整体失败、应用不启动）
docker compose -f docker-compose.prod.yml run --rm migrate
# ⑤ 应用 + workers + backup
#    首次启动或 checkpoint 表被清空后，必须先单起 memory-api 再全量：
#    LangGraph saver.setup() 首启执行 CREATE INDEX CONCURRENTLY，多进程并发
#    会互等锁卡死（P5 实测踩坑）；表建好后 setup 幂等，之后可正常全量 up。
docker compose -f docker-compose.prod.yml up -d memory-api   # 等 healthy
docker compose -f docker-compose.prod.yml up -d conversation-worker   # 首次：单建 conversation 检查点表
docker compose -f docker-compose.prod.yml up -d
# ⑥ 宿主机 nginx（首次安装站点配置后）：nginx -t && systemctl reload nginx
# ⑦ 冒烟：curl http://127.0.0.1:8000/health/ready；浏览器走 5.7 清单
```

### 备份与恢复（5.5）

backup 容器每日 03:17（UTC）逐库 `pg_dump -Fc` 到 named volume `backups`；任一失败产生 `/backups/FAILED-{yyyymmdd}` 标记（巡检发现，MVP 无主动告警）；成功则清 7 天前旧备份。恢复演练：`pg_restore` 到新建临时库，恢复后按需 `alembic upgrade`。

### 5.6 部署记录表（格式冻结；任一行未确认 → 禁止进入 5.7 真实部署）

| 项 | 值 | 确认人 | 确认日期 |
|---|---|---|---|
| postgres tag | postgres:17.11（自研叠加 pgvector 0.8.0-1，见偏离项） | 所有者 | 2026-08-31 |
| nginx（宿主机）版本 | 1.24.0 (Ubuntu) | 所有者 | 2026-08-31 |
| certbot（宿主机）版本/续期 | 证书 xueshen.xin ECDSA 至 2026-11-28；certbot.timer 自动续期 | 所有者 | 2026-08-31 |
| alpine tag + postgresql17-client apk 版本 | alpine:3.24.1 + postgresql17-client-17.11-r0 | 所有者 | 2026-08-31 |
| python 基础镜像补丁版（根 Dockerfile FROM） | python:3.13.15-slim（chore f2148b1 已提交） | 所有者 | 2026-08-31 |
| node 构建镜像补丁版 | node:24.20.0-slim | 所有者 | 2026-08-31 |
| Docker Engine / Compose 版本 | 29.7.2 / v5.5.0 | 所有者 | 2026-08-31 |
| git commit | d0e0562 | 所有者 | 2026-08-31 |
| 回滚用旧 commit | 5252d20（P5 首次拉栈基线） | 所有者 | 2026-08-31 |
| 前端产物目录 | /opt/xueshen/frontend/dist | 所有者 | 2026-08-31 |
| 域名 / CDN 域名 / DNS | xueshen.xin + www.xueshen.xin / tkkx5xhrb.hd-bkt.clouddn.com（仅 HTTP 可用，见偏离项）/ A 记录已生效 | 所有者 | 2026-08-31 |
| Kodo 五项（脱敏：只记是否已配置） | 已配置（AK/SK/Bucket=xueshenprod/Region=z0/CDN 域名；测试分桶 xueshentest） | 所有者 | 2026-08-31 |
| 管理员 UUID 名单（脱敏） | 已配置（冒烟注册账号 smoketest） | 所有者 | 2026-08-31 |
| 升配完成（2C4G + 2G swap） | 已完成（3.4Gi + 2Gi swap） | 所有者 | 2026-08-31 |
| 偏离项：宿主机 nginx/certbot 替代容器化 | 所有者已签字确认 | 所有者 | 2026-08-31 |
| 偏离项：postgres 自研镜像叠加 pgvector | RAG 链 vector 扩展需要，postgres:17.11 + postgresql-17-pgvector 0.8.0-1（deploy/postgres/Dockerfile，apt 走阿里云源、去 pgdg 源） | 所有者 | 2026-08-31 |
| 偏离项：squid 正向代理支撑镜像构建 | 国内 CDN 对 python/uv 客户端指纹限速 ~200KB/s，构建期 HTTP(S)_PROXY=http://172.17.0.1:3128（systemd 常驻，重启自启） | 所有者 | 2026-08-31 |
| 偏离项：uv.lock 全量换阿里云 PyPI 镜像 | registry + 571 个包文件直链 sed 替换（哈希不变，uv sync --frozen 审计通过） | 所有者 | 2026-08-31 |
| 偏离项：迁移后授权兜底 + LangGraph CREATE 授权 | initdb DEFAULT PRIVILEGES 去 IN SCHEMA 限定；migrate-all 增全 schema 幂等授权（仅 owner 自有对象）+ checkpoint schema CREATE 授权（memory.public / conversation.conversation_checkpoints） | 所有者 | 2026-08-31 |
| 偏离项：dcron 改常驻循环 | alpine dcron 容器内每分钟 setpgid 报错退出码 1，改 cron-loop.sh（触发语义不变：每日 UTC 03:17） | 所有者 | 2026-08-31 |
| 偏离项：qiniu SDK 7.18 适配 | put_data/delete 不再接受 timeout；set_default 须具名传参（两处 P5 冒烟实测踩坑修复） | 所有者 | 2026-08-31 |
| 待办：CDN 自定义域名 | 七牛测试域名仅 HTTP 可用（HTTPS 证书不匹配 + 403）；正式使用需在七牛控制台绑定自定义 CDN 域名（如 cdn.xueshen.xin）并配置 HTTPS 证书，然后改 .env.production 的 KODO_CDN_DOMAIN | 所有者 | 2026-08-31 |

### 环境变量

模板 `deploy/env.production.example` → `/opt/xueshen/.env.production`（chmod 600，`.env.*` 已 gitignore）。生产启动强校验：Kodo 五项 + `COMMUNITY_ADMIN_USER_IDS` + 认证密钥，缺即 Settings 构造抛错、进程不启动。

### 回滚（5.6 冻结）

先 downgrade（`alembic -c community_alembic.ini downgrade`，0002 downgrade 会清除附件/申请数据，已接受）→ 再切回旧镜像（`GIT_SHA=<旧>` 重新 `up -d`）。
