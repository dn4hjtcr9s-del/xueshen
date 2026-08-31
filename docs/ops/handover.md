# 交接说明（2026-08-31，P5 生产部署完成后）

> 面向接管开发/运维的同事。读完本文 + 按"必读顺序"过一遍列出的文件，即可独立工作。
> 本文只写"现状与踩坑"，设计语义以各冻结文档为准，冲突时以冻结文档为准。

## 一、项目一句话

MemoryManagerGraph（xueshen-math）：数学教材长期记忆 + 知识图谱 + Agentic RAG 对话 + 贴吧式社区。
FastAPI 单进程多域后端（`backend/app.py` 唯一入口）+ React(Vite) 前端，uv 管理，Python 3.13。

- 生产站点：https://xueshen.xin（已上线，2026-08-30）
- 仓库：GitHub `dn4hjtcr9s-del/xueshen`，主分支 `main`（community-rebuild 已合入）
- 当前生产部署 commit：`68d3b2f`（5.6 记录表确认版；应用镜像 tag 以服务器 `.env.production` 的 `GIT_SHA` 为准）

## 二、必读文件（按顺序）

1. **`AGENTS.md`（仓库根目录）**——命令、数据库端口、迁移链、启动方式、架构地图、提交/注释规范。**这是最高优先级的工作守则**，里面的禁令（feature flags 不擅开、scripts/ 不修存量 lint、集成测试必须用 `*_test` 库）必须遵守。
2. **`README.md`**——快速上手。
3. **`docs/ops/startup.md`**——生产部署圣经。重点看：
   - 5.2 启动顺序（含"首启必须单起 memory-api"的 LangGraph 竞态说明）；
   - **5.6 部署记录表**——所有镜像/版本冻结值 + 全部偏离项（所有者已签字）；
   - 回滚流程。
4. **`docker-compose.prod.yml` + `deploy/env.production.example`**——生产编排与全部环境变量语义。
5. **`docs/community-implementation-plan.md` / 根目录 `memory-manager-execution-spec*.md`**——社区与记忆域的设计冻结文档（代码注释里的"方案 §X"引用它们）。
6. `docs/ops/failure-runbook.md`、`docs/ops/backup-restore.md`——故障与备份恢复手册。

## 三、生产环境实况（都是实测过的）

### 服务器

- 阿里云 ECS：`root@114.215.181.134`，2C / 3.4Gi 内存 + 2Gi swap / 40G 盘，Ubuntu 24.04。
- Docker 29.7.2 + Compose v5.5.0；宿主机 nginx 1.24（反代 `127.0.0.1:8000` + 托管前端 `dist`）+ certbot 自动续期。
- 部署目录 `/opt/xueshen`（git 仓库）；密钥 `/opt/xueshen/secrets/`（私钥 0600）；环境配置 `/opt/xueshen/.env.production`（600，**只在服务器，不在仓库**）。

### 部署/变更流程（重要，和常规 push-then-pull 不同）

**服务器直接拉 GitHub 极慢，代码同步用 git bundle：**

```bash
# 本地（开发机）
git push origin main                      # 若 HTTP2 报错：git -c http.version=HTTP/1.1 push origin main
git bundle create /tmp/incN.bundle <旧sha>..main
scp -i <密钥> /tmp/incN.bundle root@114.215.181.134:/tmp/
# 服务器
cd /opt/xueshen
git fetch /tmp/incN.bundle main:refs/remotes/origin/main && git merge --ff-only origin/main
```

然后按变更类型：

- **改了后端代码**：服务器上 `sed -i "s|^GIT_SHA=.*|GIT_SHA=$(git rev-parse --short HEAD)|" .env.production` → `docker compose -f docker-compose.prod.yml build memory-api` → 给 postgres/backup 镜像补同 SHA 的 tag（内容没变就 `docker tag` 旧镜像）→ `up -d`（**首启/清空过 checkpoint 表时必须按 5.2 先单起 memory-api**）→ 更新 `docs/ops/startup.md` 5.6 表。
- **只改了 migrate-all.sh / initdb / nginx 配置**：这些是直接挂载/拷贝的，不用重建镜像。
- **改了前端**：`docker compose -f docker-compose.prod.yml run --rm frontend-build`，产物落 `/opt/xueshen/frontend/dist`，nginx 直接托管，无需重启。

### 关键基础设施：squid 构建代理

alikunlun CDN 对 python/uv 客户端指纹限速 ~200KB/s，直接构建必然超时。
宿主机 squid（3128，systemd 自启）+ compose build args 注入 `HTTP(S)_PROXY=http://172.17.0.1:3128` 解决。
**uv.lock 已全量换成阿里云 PyPI 镜像（哈希不变）**，不要在服务器上重新生成锁文件。

### 数据归属（冻结）

- **PostgreSQL（容器卷 postgres-data）**：全部文本/索引/事件/图谱/对话/社区帖子正文。
- **七牛 Kodo（xueshenprod 桶，z0 区）**：社区产生的全部文件（图片等）。
- 记忆正文 Markdown 在 memory-data 卷（/data/memory）。

### 数据库

单 PostgreSQL 实例（自研镜像 postgres:17.11 + pgvector 0.8.0-1）承载五库：
`memory` / `auth` / `conversation` / `community` / `rag`（study 未启用不建）。
每库双角色：`<db>_owner`（迁移）/ `<db>_app`（运行时最小权限）。
六条 Alembic 链，生产由 `scripts/migrate-all.sh` 统一跑（含授权兜底段）。

## 四、账号与权限现状

| 账号 | 角色 | 说明 |
|---|---|---|
| `zjh` | **社区唯一管理员**（COMMUNITY_ADMIN_USER_IDS） | 所有者正式账号 |
| `smoketest` | 普通用户 | P5 冒烟注册，留有测试吧「搞笑社区」+ 一条测试帖；是否清理由所有者决定 |

生产强校验（缺即进程不启动）：Kodo 五项、`COMMUNITY_ADMIN_USER_IDS`、RSA 密钥、
`DEV_AUTH_ENABLED=false`、RAG 三个模型角色（rewrite/evidence/answer）等——改配置失败先看 Settings 校验报错。

## 五、已知遗留事项（按优先级）

1. **CDN 自定义域名未绑（影响图片 HTTPS）**：七牛测试域名 `tkkx5xhrb.hd-bkt.clouddn.com` 仅 HTTP 可用。
   需要所有者在七牛控制台绑自定义域名（如 cdn.xueshen.xin）+ 配证书，然后改 `.env.production` 的
   `KODO_CDN_DOMAIN` 并重启 memory-api。
2. **备份恢复演练未做**（5.5 验收最后一项）：`pg_restore` 最近一次 dump 到临时库验证。
3. **服务器安全加固**：22 端口对全网开放且允许密码登录，建议改仅密钥 + 收敛安全组。
4. **七牛 AK/SK 轮换**：曾在聊天中明文传输，建议控制台轮换后更新 `.env.production`。
5. **smoketest 测试数据清理**（待所有者决定）。
6. 上传接口返回的 `size_bytes` 恒为 0（非阻塞小 bug，社区附件服务里取大小逻辑待修）。

## 六、开发工作流要点

- 本地 CI 统一入口：`scripts/ci-local.sh [stage ...]`；改后端至少跑 `backend-lint` + `backend-unit`。
- 契约测试：路由/schema 变更后 `UPDATE_OPENAPI_SNAPSHOT=1 .venv/bin/python -m pytest tests/contract -q` 更新快照。
- 集成测试：`scripts/ci-local.sh backend-integration`（自动建 `*_test` 库）。
- 前端：`cd frontend && npm run dev / lint / test / build`。
- Commit 规范：`feat|fix|chore(域): 中文描述`；注释/docstring 全简体中文。
- Feature flags（`COMMUNITY_PUBLISHER_ENABLED` 等三条灰度链路、study 域）：**保持 false，启用必须等所有者批准**。
- Ruff 行宽 100；mypy strict 只管 `backend/`；`scripts/` 存量 lint 错误不要顺手修。

## 七、P5 部署踩坑清单（已全部修复并固化，别再踩）

| 坑 | 修复位置 |
|---|---|
| CDN 对 uv/python 客户端指纹限速 | 宿主机 squid + compose build args 代理 |
| `UV_INDEX_URL` 对 `--frozen` 无效 | uv.lock 直接换阿里云源 |
| 生产 Settings 强校验缺 DEV_AUTH=false / RSA 密钥 / RAG 模型角色 | `.env.production` + `deploy/env.production.example` |
| 镜像漏打包 rag 迁移链 | 根 `Dockerfile` COPY rag_* |
| rag 链需 vector 扩展 | `deploy/postgres/Dockerfile`（pgvector）+ initdb 预建扩展 |
| apt.postgresql.org / deb.debian.org 国内卡死 | 去 pgdg 源 + apt 换阿里云镜像 |
| initdb 默认权限只管 public schema | 去 `IN SCHEMA` 限定 + migrate-all 授权兜底 |
| LangGraph saver.setup() 需 schema CREATE 且多进程首启竞态互锁 | migrate-all 授权段 + startup.md 5.2 启动顺序 |
| dcron 容器内 setpgid 崩溃循环 | `deploy/backup/cron-loop.sh` 常驻循环替代 |
| qiniu 7.18 put_data/delete 不收 timeout；set_default 是具名签名 | `backend/community/storage/kodo.py` |
| 旧管理员撤销 | COMMUNITY_ADMIN_USER_IDS 已切到 zjh |

## 八、常用运维命令速查

```bash
ssh -i <密钥> root@114.215.181.134
cd /opt/xueshen
docker compose -f docker-compose.prod.yml ps                 # 全栈状态
curl -s http://127.0.0.1:8000/health/ready                   # 就绪探针（failures 字段定位问题）
docker logs xueshen-prod-memory-api-1 --tail 50              # 应用日志
docker exec xueshen-prod-postgres-1 psql -U postgres -d <库> # 进库
docker exec xueshen-prod-backup-1 cat /var/log/backup.log    # 备份日志
ls /var/lib/docker/volumes/xueshen-prod_backups/_data        # 备份文件（留意 FAILED-* 标记）
```
