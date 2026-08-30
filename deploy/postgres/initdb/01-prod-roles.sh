#!/bin/bash
# 生产 postgres 五库初始化（community-rebuild-plan.md 5.1/5.6：库名与角色命名默认值冻结）。
# 仅由 docker-entrypoint-initdb.d 在空数据目录首次启动时执行一次。
#
# 每库两类角色（密码全部来自环境变量，不写入本文件、不落盘）：
#   <db>_owner：迁移用（属主，DDL 权限），migrate job 经 *_MIGRATE_DATABASE_URL 连接；
#   <db>_app  ：运行时用（最小权限），应用经 *_DATABASE_URL 连接。
# ALTER DEFAULT PRIVILEGES 保证迁移新建的表/序列自动授权给对应 app 角色。
#
# 库清单（冻结）：memory / auth / conversation / community / rag（study MVP 不启用不建）。
set -euo pipefail

: "${PG_MEMORY_OWNER_PASSWORD:?缺少环境变量}"  "${PG_MEMORY_APP_PASSWORD:?缺少环境变量}"
: "${PG_AUTH_OWNER_PASSWORD:?缺少环境变量}"    "${PG_AUTH_APP_PASSWORD:?缺少环境变量}"
: "${PG_CONVERSATION_OWNER_PASSWORD:?缺少环境变量}" "${PG_CONVERSATION_APP_PASSWORD:?缺少环境变量}"
: "${PG_COMMUNITY_OWNER_PASSWORD:?缺少环境变量}"  "${PG_COMMUNITY_APP_PASSWORD:?缺少环境变量}"
: "${PG_RAG_OWNER_PASSWORD:?缺少环境变量}"     "${PG_RAG_APP_PASSWORD:?缺少环境变量}"

setup_db() {
    local db="$1" owner_pw="$2" app_pw="$3"
    local owner="${db}_owner" app="${db}_app"

    psql -v ON_ERROR_STOP=1 -U postgres <<SQL
CREATE ROLE ${owner} LOGIN PASSWORD '${owner_pw}';
CREATE ROLE ${app} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD '${app_pw}';
CREATE DATABASE ${db} OWNER ${owner};
REVOKE CONNECT ON DATABASE ${db} FROM PUBLIC;
GRANT CONNECT ON DATABASE ${db} TO ${owner};
GRANT CONNECT ON DATABASE ${db} TO ${app};
-- owner 迁移新建对象自动授权 app（表 DML + 序列自增）。
-- 不加 IN SCHEMA 限定 = 全局默认权限：迁移会创建非 public schema
-- （memory.ops / conversation.conversation / rag.rag 等），限定 public 会导致
-- app 角色读不到新 schema 的表（P5 实测踩坑：维护闸门读 ops.system_maintenance 失败）。
ALTER DEFAULT PRIVILEGES FOR ROLE ${owner}
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO ${app};
ALTER DEFAULT PRIVILEGES FOR ROLE ${owner}
    GRANT USAGE, SELECT ON SEQUENCES TO ${app};
SQL

    # 目标库内的 schema 级授权（对 initdb 时点之后 owner 创建的对象由 DEFAULT PRIVILEGES 覆盖）
    psql -v ON_ERROR_STOP=1 -U postgres -d "$db" <<SQL
GRANT USAGE ON SCHEMA public TO ${app};
SQL

    echo "[initdb] ${db}: owner=${owner} app=${app} 完成"
}

setup_db memory        "$PG_MEMORY_OWNER_PASSWORD"        "$PG_MEMORY_APP_PASSWORD"
setup_db auth          "$PG_AUTH_OWNER_PASSWORD"          "$PG_AUTH_APP_PASSWORD"
setup_db conversation  "$PG_CONVERSATION_OWNER_PASSWORD"  "$PG_CONVERSATION_APP_PASSWORD"
setup_db community     "$PG_COMMUNITY_OWNER_PASSWORD"     "$PG_COMMUNITY_APP_PASSWORD"
setup_db rag           "$PG_RAG_OWNER_PASSWORD"           "$PG_RAG_APP_PASSWORD"

# rag 链迁移含 CREATE EXTENSION vector（pgvector），owner 非超级用户无权创建，
# 由 initdb 超级用户预建（迁移 SQL 带 IF NOT EXISTS，幂等兼容；P5 实测踩坑）
psql -v ON_ERROR_STOP=1 -U postgres -d rag -c "CREATE EXTENSION IF NOT EXISTS vector;"
echo "[initdb] rag: vector 扩展已预建"

echo "[initdb] 五库初始化完成"
