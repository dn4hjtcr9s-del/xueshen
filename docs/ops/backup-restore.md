# 备份与恢复手册（§21.4）

## 策略

- 保留：30 天；第一版不承诺 PITR（需要更低 RPO 时再启用 WAL 归档）。
- 每日一批：PostgreSQL 逻辑备份（`pg_dump -Fc`）+ Markdown 存储 tar.gz + manifest.json，三者共用一个 `backup_runs.batch_id`。
- 加密：`age-x25519-v1`。postgres/markdown 产物加密；manifest 不含用户内容、保持明文以便恢复前校验。
- 产物布局：`{BACKUP_ROOT}/{batch_id}/{postgres.dump.age, markdown.tar.gz.age, manifest.json}`；写入先落 `{batch_id}.tmp/` 再原子改名。
- Scheduler 不执行备份，只在每天 05:00（Asia/Shanghai）检查 `backup_runs`：当天无成功批次则告警日志。

## 密钥管理

```bash
age-keygen -o .local/backup-age-key.txt   # 生成密钥对（.local/ 已 gitignore）
# 公钥写入 BACKUP_AGE_RECIPIENT，私钥文件路径写入 BACKUP_AGE_IDENTITY_FILE
```

私钥丢失 = 全部备份不可恢复。生产环境私钥应离线另存一份。本机开发密钥：`.local/backup-age-dev-key.txt`。

## 每日备份（宿主机 cron）

```cron
17 4 * * * cd /path/to/xueshen && ./scripts/backup.sh >> .local/backups/backup.log 2>&1
```

- 脚本经 `docker compose exec -T postgres pg_dump` 取数，依赖运行中的 postgres 容器。
- 任一环节失败：批次记 `failed` + `error_summary`，临时目录自动清理；当天需人工重跑。

## 每周恢复验证

```bash
./scripts/backup.sh                       # 或挑选最近成功批次
uv run python -m backend.memory.cli verify-backup-restore --batch-id <uuid>
```

在隔离临时目录解密并校验 manifest 与两份产物 checksum，结果写回
`backup_runs.restore_verification_status/restore_verified_at/restore_verification_error`。

## 覆盖性恢复（灾难恢复）

```bash
./scripts/restore.sh --batch-id <uuid>            # 默认只允许空目标
./scripts/restore.sh --batch-id <uuid> --force    # 显式覆盖现有环境或接管失败恢复
```

恢复入口在写入 live 目标前会完成 manifest、migration revision、加密产物和
checksum 校验。真正开始覆盖恢复时，它会在数据库级获取全局恢复互斥锁并持久化
`ops.system_maintenance.active=true`，然后等待已有流量排空。以下所有进程在门禁激活
期间都 fail-closed：

- API（包括 `/health/live`、`/health/startup`、`/health/ready`）返回 503；
- Worker 停止领取和执行 operation；
- Outbox Consumer 停止领取和投递；
- Scheduler 停止调度维护任务。

门禁保持期间，恢复流程依次检查目标、重置 `public` schema、执行 `pg_restore`、确认
备份 revision 与恢复出的 `alembic_version` 一致、自动 `alembic upgrade head`、再次确认
当前唯一 head、恢复 Markdown，并同步重放账号删除 ledger。全部步骤和删除重放成功后
才会清除门禁并开放流量。

任一步骤失败时，门禁会故意保持 active，数据必须视为不可信，不能通过手工直接清除
`ops.system_maintenance` 来开放流量。确认失败原因并准备完整重做后，使用同批次
`--force` 接管残留恢复锁状态；接管后仍须从 manifest 校验开始完整执行，不能只手工
重放 purge 或只执行 migration。

目标非空但未指定 `--force` 属于安全拒绝，不会执行覆盖写入；确认环境后再显式重试。

## 单用户恢复演练


按批次整体恢复后，从恢复环境中按 `user_id` 导出数据库行与对应 Markdown（`users/{id前2位}/{id}/`）即可；第一版不提供独立的单用户恢复命令。

## 本机演练记录

- 2026-08-11：`scripts/backup.sh` 批次 `a0e670b0-2e35-42bf-ad3f-5c4fd437b50d` 成功；恢复到独立库 `memory_restore_drill`，`knowledge_graph_nodes=133`、`alembic_version=0003_operation_commit_started`、`backup_runs` 行齐备，与源库一致；演练库已删除。
