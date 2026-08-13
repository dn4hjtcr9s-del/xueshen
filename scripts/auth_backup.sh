#!/usr/bin/env bash
# auth 库独立逻辑备份（方案 §10.2 / 附录 A.5 #18）。
#
# 与 memory 备份（scripts/backup.sh）相互独立。灾难恢复顺序必须 auth 先于 memory
# （身份源先就位），恢复后执行一致性检查：
#   bash scripts/auth_backup.sh --check   # auth users ↔ memory identity mappings 双向核对
#
# 用法：
#   bash scripts/auth_backup.sh           # 执行备份 + 恢复校验（临时库 pg_restore）
#   bash scripts/auth_backup.sh --check   # 只做双向一致性检查
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_ROOT="${BACKUP_ROOT:-.local/backups}"
AUTH_DB_USER="${AUTH_DB_USER:-auth}"
AUTH_DB_NAME="${AUTH_DB_NAME:-auth}"
ADMIN_USER="${POSTGRES_ADMIN_USER:-postgres}"

check_consistency() {
  # 双向核对（复审 P2-8）：
  #   1) auth 用户 → memory 映射（pending 补偿事件除外）
  #   2) memory 映射 → auth 用户
  #   3) 映射归属：internal_user_id 必须等于 external_subject
  echo "== 一致性检查：auth users ↔ memory identity mappings =="
  USERS="$(docker compose exec -T postgres psql -U "$AUTH_DB_USER" -d auth -tAc \
    "SELECT user_id::text FROM users ORDER BY 1")"
  SUBS="$(docker compose exec -T postgres psql -U memory -d memory -tAc \
    "SELECT external_subject FROM account_identity_mappings WHERE issuer='gewu-auth' ORDER BY 1")"
  PENDING="$(docker compose exec -T postgres psql -U "$AUTH_DB_USER" -d auth -tAc \
    "SELECT user_id::text FROM identity_mapping_outbox WHERE status='pending' ORDER BY 1")"
  MISMATCH="$(docker compose exec -T postgres psql -U memory -d memory -tAc \
    "SELECT external_subject || ' -> ' || internal_user_id::text \
       FROM account_identity_mappings \
      WHERE issuer='gewu-auth' AND internal_user_id::text <> external_subject \
      ORDER BY 1")"

  MISSING_MAPPING="$(comm -23 <(echo "$USERS") <(printf '%s\n%s\n' "$SUBS" "$PENDING" | sort -u))"
  ORPHAN_MAPPING="$(comm -13 <(echo "$USERS") <(echo "$SUBS"))"

  if [[ -z "$MISSING_MAPPING" && -z "$ORPHAN_MAPPING" && -z "$MISMATCH" ]]; then
    echo "  一致：无缺失映射，无孤儿映射，映射归属全部正确。"
  else
    [[ -n "$MISSING_MAPPING" ]] && echo "  缺失映射的用户: $MISSING_MAPPING"
    [[ -n "$ORPHAN_MAPPING" ]] && echo "  无对应用户的映射行: $ORPHAN_MAPPING"
    [[ -n "$MISMATCH" ]] && echo "  归属不一致的映射行(external -> internal): $MISMATCH"
    return 1
  fi
}

if [[ "${1:-}" == "--check" ]]; then
  check_consistency
  exit 0
fi

mkdir -p "$BACKUP_ROOT/auth"
STAMP="$(date +%Y%m%d-%H%M%S)"
DUMP="$BACKUP_ROOT/auth/auth-$STAMP.dump"

# 1. 逻辑备份（自定义格式）
docker compose exec -T postgres pg_dump -U "$AUTH_DB_USER" -Fc "$AUTH_DB_NAME" > "$DUMP"

# 2. 恢复校验：临时库 pg_restore 完整还原后即删除（不污染任何数据）
CHECK_DB="auth_backup_check_${STAMP}"
docker compose exec -T postgres createdb -U "$ADMIN_USER" "$CHECK_DB"
docker compose exec -T postgres psql -U "$ADMIN_USER" -d "$CHECK_DB" -v ON_ERROR_STOP=1 \
  -c "GRANT ALL ON SCHEMA public TO $AUTH_DB_USER;" >/dev/null
docker compose exec -T postgres pg_restore -U "$AUTH_DB_USER" -d "$CHECK_DB" < "$DUMP"
docker compose exec -T postgres dropdb -U "$ADMIN_USER" "$CHECK_DB"

echo "auth 备份完成并通过恢复校验: $DUMP"
echo "注意：灾难恢复顺序必须 auth 先于 memory，随后执行 --check 双向核对。"
