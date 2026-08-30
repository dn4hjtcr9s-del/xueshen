#!/bin/sh
# 生产逐库备份 wrapper（community-rebuild-plan.md 5.5 冻结语义）：
# - 逐库 pg_dump 自定义格式到 /backups/{db}-{yyyymmdd}.dump；
# - 任一失败 → 错误日志 + 创建 /backups/FAILED-{yyyymmdd} 标记 + 脚本非零退出
#   （cron 任务级失败；backup 容器常驻不退出；失败发现 = runbook 巡检 FAILED 标记）；
# - 全部成功 → 删除当日 FAILED 标记 + 清理 7 天前旧备份；
# - 备份文件 0600；库清单默认四库 + rag（study 未启用不备份）。
set -u

DATE=$(date +%Y%m%d)
FAILED_MARK="/backups/FAILED-${DATE}"
STATUS=0

umask 077

dump_one() {
    db="$1"
    out="/backups/${db}-${DATE}.dump"
    echo "[backup] ${db} -> ${out}"
    if PGPASSWORD="${POSTGRES_PASSWORD:?缺少 POSTGRES_PASSWORD}" \
        pg_dump -h postgres -U postgres -d "$db" -Fc -f "$out"; then
        chmod 600 "$out"
    else
        echo "[backup] ${db} 备份失败" >&2
        : > "$FAILED_MARK"
        STATUS=1
    fi
}

dump_one memory
dump_one auth
dump_one conversation
dump_one community
dump_one rag

if [ "$STATUS" -eq 0 ]; then
    rm -f "$FAILED_MARK"
    find /backups -name '*.dump' -mtime +7 -delete
    echo "[backup] 全部成功，已清理 7 天前旧备份"
else
    echo "[backup] 存在失败库，标记 ${FAILED_MARK}" >&2
fi

exit "$STATUS"
