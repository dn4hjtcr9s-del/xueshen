#!/usr/bin/env bash
# 恢复入口（规格 §21.4）：校验 manifest/checksum/加密元数据后写入目标；
# 默认只允许空目标，覆盖现有环境必须显式 --force。
# 用法：./scripts/restore.sh --batch-id <uuid> [--force]
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run python -m backend.memory.cli restore-backup "$@"
