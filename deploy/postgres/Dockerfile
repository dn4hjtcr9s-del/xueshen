# 生产 PostgreSQL 镜像：postgres:17.11 + pgvector（RAG 链迁移需要 vector 扩展，P5 实测踩坑）。
# 基础镜像经 build arg 固定补丁版（禁浮动 tag，版本抄录进 startup.md 5.6 部署记录表）；
# pgvector 用 Debian 官方源 postgresql-17-pgvector 包，安装后 dpkg -l 抄录确切版本。
ARG POSTGRES_IMAGE=postgres:17.11
FROM ${POSTGRES_IMAGE}

# 构建期可走宿主机 squid 正向代理（compose build args 注入，与 backend 镜像同一策略）
ARG HTTP_PROXY
ARG HTTPS_PROXY

# pgvector 已在 Debian 官方源（trixie/main），临时移除镜像自带的 pgdg 源：
# apt.postgresql.org 国内访问近乎不可用（P5 实测 6.7KB 索引卡死 7 分钟+）。
# deb.debian.org 经代理同样不稳定，构建期换阿里云镜像（仅镜像内 apt，与运行时无关）。
RUN rm -f /etc/apt/sources.list.d/pgdg.list \
    && sed -i 's|//deb.debian.org|//mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-17-pgvector \
    && rm -rf /var/lib/apt/lists/*