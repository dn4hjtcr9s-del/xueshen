#!/bin/sh
# 常驻备份触发循环（替代 dcron：alpine dcron 在本容器环境每分钟报
# "setpgid: Operation not permitted" 后退出码 1 崩溃循环，P5 实测踩坑）。
# 语义与原 crontab 一致：每天 UTC 03:17（北京时间 11:17）触发一次 backup.sh，
# 触发后 sleep 70s 防止同一分钟重复执行。
set -u

echo "[cron-loop] 备份调度循环已启动（每日 UTC 03:17 触发）"
while :; do
    if [ "$(date -u +%H%M)" = "0317" ]; then
        echo "[cron-loop] 触发每日备份 $(date -u '+%Y-%m-%d %H:%M UTC')"
        if /usr/local/bin/backup.sh >> /var/log/backup.log 2>&1; then
            echo "[cron-loop] 备份完成"
        else
            echo "[cron-loop] 备份失败（详见 /var/log/backup.log 与 /backups/FAILED-* 标记）"
        fi
        sleep 70
    fi
    sleep 20
done
