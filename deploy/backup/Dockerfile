# 生产备份镜像（community-rebuild-plan.md 5.5）：alpine + postgresql-client + dcron。
# ALPINE_IMAGE 由 compose build args 注入（补丁版 tag 执行时抄录进 5.6 部署记录表，
# 禁止浮动 tag）；apk 实际安装的 postgresql-client 版本同样在部署时抄录。
ARG ALPINE_IMAGE
FROM ${ALPINE_IMAGE}

RUN apk add --no-cache dcron postgresql17-client

COPY backup.sh /usr/local/bin/backup.sh
COPY crontab /etc/crontabs/root

RUN chmod 755 /usr/local/bin/backup.sh && chmod 600 /etc/crontabs/root

# dcron 前台运行；备份失败只影响当次 cron 任务（容器常驻，靠 FAILED 标记 + 巡检发现）
CMD ["crond", "-f", "-l", "6"]
