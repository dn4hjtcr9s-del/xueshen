# 生产备份镜像（community-rebuild-plan.md 5.5）：alpine + postgresql-client + 常驻触发循环。
# ALPINE_IMAGE 由 compose build args 注入（补丁版 tag 执行时抄录进 5.6 部署记录表，
# 禁止浮动 tag）；apk 实际安装的 postgresql-client 版本同样在部署时抄录。
# 原设计 dcron 已废弃：本容器环境 crond 每分钟 setpgid 报错后退出码 1（P5 实测踩坑），
# 改为 cron-loop.sh 常驻循环，触发语义与原 crontab 一致（每日 UTC 03:17）。
ARG ALPINE_IMAGE
FROM ${ALPINE_IMAGE}

RUN apk add --no-cache postgresql17-client

COPY backup.sh /usr/local/bin/backup.sh
COPY cron-loop.sh /usr/local/bin/cron-loop.sh

RUN chmod 755 /usr/local/bin/backup.sh /usr/local/bin/cron-loop.sh

# 常驻循环前台运行；备份失败只影响当次任务（容器常驻，靠 FAILED 标记 + 巡检发现）
CMD ["/usr/local/bin/cron-loop.sh"]
