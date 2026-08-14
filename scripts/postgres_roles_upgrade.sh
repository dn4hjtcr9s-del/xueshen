#!/usr/bin/env bash
# 存量 postgres-data volume 的四账号隔离升级脚本（方案 §6.1 / 附录 A.1 #4，Community §4.2）。
# 全新部署无需运行：docker-entrypoint-initdb.d 首次启动自动执行同一套初始化。
# 幂等：已升级的 volume 重复执行不产生副作用；不删除、不修改任何业务数据。
#
# 原理：旧 compose 以 POSTGRES_USER=memory 初始化，memory 是集群引导超级用户，
# 而 PostgreSQL 规定引导超级用户永远不能失去 SUPERUSER 属性，无法直接降权。
# 因此采用改名交换：memory 改名 memory_bootstrap 并 NOLOGIN 锁死，新建普通角色
# memory，并把 memory 库内应用对象所有权逐一迁移过去。
#
# 用法：bash scripts/postgres_roles_upgrade.sh
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose up -d postgres

# 等待 postgres 可接受连接（最多 30s）
for _ in $(seq 1 30); do
  if docker compose exec -T postgres pg_isready -q; then
    break
  fi
  sleep 1
done

CONTAINER="$(docker compose ps -q postgres)"

# 统一 psql 入口：docker exec -i 转发 stdin，支持 heredoc 脚本
psql_as() {
  local user=$1 db=$2
  shift 2
  docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -U "$user" -d "$db" "$@"
}

# 1. 确保 postgres 管理员存在（存量 volume 的引导超级用户是 memory，可能没有该角色）
if ! psql_as postgres template1 -tAc "SELECT 1" >/dev/null 2>&1; then
  docker exec -i "$CONTAINER" psql -v ON_ERROR_STOP=1 -U memory -d memory \
    -c "CREATE ROLE postgres LOGIN SUPERUSER CREATEDB CREATEROLE REPLICATION PASSWORD 'postgres';"
fi

# 2. 收敛管理员与 auth 角色状态
psql_as postgres template1 \
  -c "ALTER ROLE postgres WITH LOGIN SUPERUSER CREATEDB CREATEROLE REPLICATION PASSWORD 'postgres';"
psql_as postgres template1 -c "DO \$\$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'auth') THEN
    CREATE ROLE auth LOGIN PASSWORD 'auth';
END IF; END \$\$;"
psql_as postgres template1 \
  -c "ALTER ROLE auth WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'auth';"
psql_as postgres template1 -c "DO \$\$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'conversation') THEN
    CREATE ROLE conversation LOGIN PASSWORD 'conversation';
END IF; END \$\$;"
psql_as postgres template1 \
  -c "ALTER ROLE conversation WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'conversation';"
psql_as postgres template1 -c "DO \$\$ BEGIN
IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'community') THEN
    CREATE ROLE community LOGIN PASSWORD 'community';
END IF; END \$\$;"
psql_as postgres template1 \
  -c "ALTER ROLE community WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'community';"

# 3. 按 memory 角色当前状态分类处理
STATE="$(psql_as postgres template1 -tAc "SELECT CASE
    WHEN NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'memory') THEN 'missing'
    WHEN (SELECT rolsuper FROM pg_roles WHERE rolname = 'memory') THEN 'legacy_super'
    ELSE 'ok' END;")"

case "$STATE" in
  missing)
    # 全新 volume 但未走 initdb 路径（罕见）：直接建普通角色
    psql_as postgres template1 \
      -c "CREATE ROLE memory LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'memory';"
    ;;
  legacy_super)
    # 改名交换：必须由 postgres 管理员执行（会话用户不能给自己改名）
    psql_as postgres template1 -c "ALTER ROLE memory RENAME TO memory_bootstrap;"
    psql_as postgres template1 -c "ALTER ROLE memory_bootstrap NOLOGIN;"
    psql_as postgres template1 \
      -c "CREATE ROLE memory LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'memory';"
    if psql_as postgres template1 -tAc "SELECT 1 FROM pg_database WHERE datname = 'memory'" | grep -q 1; then
      psql_as postgres template1 -c "ALTER DATABASE memory OWNER TO memory;"
      # 迁移 memory 库内应用对象所有权。引导超级用户拥有系统对象，
      # REASSIGN OWNED 会被系统拒绝，故逐对象枚举迁移。
      psql_as postgres memory <<'SQL'
DO $$
DECLARE
  r record;
  kind text;
BEGIN
  FOR r IN
    SELECT n.nspname AS sch, c.relname AS name,
           CASE c.relkind
             WHEN 'r' THEN 'TABLE' WHEN 'p' THEN 'TABLE'
             WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
             WHEN 'm' THEN 'MATERIALIZED VIEW'
           END AS k
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relowner = (SELECT oid FROM pg_roles WHERE rolname = 'memory_bootstrap')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  LOOP
    IF r.k IS NOT NULL THEN
      EXECUTE format('ALTER %s %I.%I OWNER TO memory', r.k, r.sch, r.name);
    END IF;
  END LOOP;

  FOR r IN
    SELECT nspname AS sch FROM pg_namespace
    WHERE nspowner = (SELECT oid FROM pg_roles WHERE rolname = 'memory_bootstrap')
      AND nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
  LOOP
    EXECUTE format('ALTER SCHEMA %I OWNER TO memory', r.sch);
  END LOOP;

  FOR r IN
    SELECT n.nspname AS sch, p.proname AS name,
           pg_get_function_identity_arguments(p.oid) AS args
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE p.proowner = (SELECT oid FROM pg_roles WHERE rolname = 'memory_bootstrap')
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  LOOP
    EXECUTE format('ALTER FUNCTION %I.%I(%s) OWNER TO memory', r.sch, r.name, r.args);
  END LOOP;
END $$;
SQL
    fi
    ;;
  ok)
    # 已升级：无需处理
    ;;
esac

# 4. 建库与授权收敛（幂等）
psql_as postgres template1 <<'SQL'
SELECT 'CREATE DATABASE memory OWNER memory'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'memory')\gexec
SELECT 'CREATE DATABASE auth OWNER auth'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'auth')\gexec
SELECT 'CREATE DATABASE conversation OWNER conversation'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'conversation')\gexec
SELECT 'CREATE DATABASE community OWNER community'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'community')\gexec
REVOKE CONNECT ON DATABASE memory FROM PUBLIC;
GRANT CONNECT ON DATABASE memory TO memory;
REVOKE CONNECT ON DATABASE auth FROM PUBLIC;
GRANT CONNECT ON DATABASE auth TO auth;
REVOKE CONNECT ON DATABASE conversation FROM PUBLIC;
GRANT CONNECT ON DATABASE conversation TO conversation;
REVOKE CONNECT ON DATABASE community FROM PUBLIC;
GRANT CONNECT ON DATABASE community TO community;
SQL

echo "五个账号隔离升级完成：管理员=postgres；应用账号=memory、auth、conversation、community（均为非超级用户）"
