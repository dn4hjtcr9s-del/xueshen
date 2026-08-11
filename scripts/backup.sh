#!/usr/bin/env bash
# 每日备份入口（规格 §21.4）：PostgreSQL 逻辑备份 + Markdown tar + age 加密，
# 产物写入 BACKUP_ROOT（默认 .local/backups），状态记入 backup_runs。
# 建议宿主机 cron 每日执行，例如：
#   17 4 * * * cd /path/to/xueshen && ./scripts/backup.sh >> .local/backups/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python -m backend.memory.cli create-backup "$@"
