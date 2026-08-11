# 故障处理手册（Runbook）

所有日志为 JSON 行（`ts/level/logger/message`）；`user_id` 只以
`HMAC-SHA256(LOG_HMAC_KEY, user_id)` 摘要出现，需定位用户时用同一 key 计算摘要后检索。

## 1. Worker 宕机 / operation 卡死

现象：operation 长时间 `running`、用户操作无结果。

机制：Worker 崩溃后 Lease 过期，Scheduler 自动回收过期 Lease（日志
`回收过期 operation Lease：N 个`），operation 回到队列由其他 Worker 领取；
mutation 幂等（`mutation_id` + `expected_version`），恢复执行不会重复提交。

处理：
1. 确认 memory-worker 进程存活；不在则重启，无需人工清理队列。
2. 回收后仍反复失败的 operation 会进入 `retry_wait`（指数退避），最终 `failed`；
   用 `GET /api/v1/memory/operations/{id}` 查 `public_error`。
3. 进程反复崩溃：看 `memory.worker` 日志 `operation 执行出现未捕获异常` 与
   `hard timeout`；常见原因是模型 API 不可用或数据库连接耗尽。

## 2. Outbox 积压 / 通知不到达

现象：用户通知延迟、`memory_outbox` 行数持续增长。

机制：Outbox Consumer 至少一次投递；部分目标失败按目标独立重试，
`dispatch` 与 `mark_delivery` 同事务，崩溃后重投不产生重复通知。

处理：
1. 确认 memory-outbox-consumer 进程存活。
2. 查日志 `outbox 处理出现未捕获异常`；投递失败会按退避重试，最终记 failed。
3. 投影（summary_projection）异常：失败不产生 Overlay/审计，重试幂等；
   重复投递按幂等成功结束，不会重复应用图谱状态。

## 3. 备份告警

现象：Scheduler 日志 `告警：当天（YYYY-MM-DD）无成功的 backup_runs 记录`。

处理：
1. 查 `backup_runs` 当天行：`status='failed'` 时看 `error_summary`
   （常见：postgres 容器未运行、`BACKUP_AGE_RECIPIENT` 未配置、磁盘满）。
2. 手工重跑 `./scripts/backup.sh`；成功后告警次日消失。
3. 无当天行 = cron 未执行：检查宿主机 cron 与 `backup.log`。

## 4. /health/ready 失败

| failure | 含义与处理 |
| --- | --- |
| `storage_not_writable` | `MEMORY_STORAGE_ROOT` 不可写；检查目录权限与磁盘 |
| `database_unavailable` | PostgreSQL 未启动或 `DATABASE_URL` 错误；`docker compose up -d postgres` |
| `migration_version_mismatch` | 库版本落后于代码；`uv run alembic upgrade head` |
| `knowledge_graph_registry_not_loaded` | 图谱注册表为空；`uv run python -m backend.memory.cli sync-knowledge-graph --apply` |
| `production_auth_not_configured` | 生产认证参数缺失；配置 JWT iss/aud/公钥或 JWKS |

## 5. 数据库连接 / 锁问题

- `DATABASE_STATEMENT_TIMEOUT_MS=150000`、`DATABASE_LOCK_TIMEOUT_MS=10000` 已在会话级设置；
  长语句被终止时按 operation 重试机制自动恢复。
- 连接耗尽：API/Worker/Scheduler 各有连接池，先查是否有进程泄漏长事务
  （`pg_stat_activity`），必要时重启对应进程。

## 6. Markdown 存储与物化

- current 物化副本损坏或丢失不影响正确性：读取以数据库活动指针 + 不可变版本为准，
  物化缺失时可按 `memory_id + version` 从版本区重建。
- `quarantine/` 保存 30 天删除窗口；`purge_tombstones` 每日 03:00 清理过期 tombstone。

## 7. Break-glass 应急访问

管理员默认不可读正文。故障/申诉需要时：

```bash
uv run python -m backend.memory.cli create-break-glass-grant \
  --admin-user-id <admin> --target-user-id <user> \
  --reason "<原因>" --scopes "memory:read" --minutes 30 [--approved-by <approver>]
```

- 最长 60 分钟；生产环境申请者与批准者必须不同（`--approved-by` 必填）。
- 使用时带请求头 `X-Break-Glass-Grant-Id: <grant_id>`；grant 只对属主 admin 有效。
- 申请、批准、使用、过期检查与每次正文读/写都写入 `memory_break_glass_audit`；
  事后审计：`SELECT * FROM memory_break_glass_audit WHERE grant_id = ...`。
- 提前结束权限：`uv run python -m backend.memory.cli revoke-break-glass-grant --grant-id <uuid>`。

## 8. 灾难恢复

见 backup-restore.md「覆盖性恢复」。要点：恢复后必须重放账号删除 manifest，
否则已删除账号数据会复活；`/health/ready` 全绿后再开放流量。
