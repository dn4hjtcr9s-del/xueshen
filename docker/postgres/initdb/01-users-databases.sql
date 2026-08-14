-- 全新 volume 四账号初始化（方案 §6.1 / 附录 A.1 #2 #3，Community §4.2）。
-- 仅由 docker-entrypoint-initdb.d 在空数据目录首次启动时自动执行（此时引导超级用户
-- 为 postgres，memory / auth / conversation / community 尚未存在）。
-- 存量 volume 升级请用 scripts/postgres_roles_upgrade.sh；勿在存量库上直接执行本文件
-- （ALTER ROLE memory NOSUPERUSER 对旧引导超级用户会被 PostgreSQL 拒绝）。

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'memory') THEN
        CREATE ROLE memory LOGIN PASSWORD 'memory';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'auth') THEN
        CREATE ROLE auth LOGIN PASSWORD 'auth';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'conversation') THEN
        CREATE ROLE conversation LOGIN PASSWORD 'conversation';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'community') THEN
        CREATE ROLE community LOGIN PASSWORD 'community';
    END IF;
END $$;

ALTER ROLE memory WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'memory';
ALTER ROLE auth WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'auth';
ALTER ROLE conversation WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'conversation';
ALTER ROLE community WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD 'community';

SELECT 'CREATE DATABASE memory OWNER memory'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'memory')\gexec

SELECT 'CREATE DATABASE auth OWNER auth'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'auth')\gexec

SELECT 'CREATE DATABASE conversation OWNER conversation'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'conversation')\gexec

SELECT 'CREATE DATABASE community OWNER community'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'community')\gexec

-- 数据库级隔离：撤销 PUBLIC 连接权，仅各自所有者可连（超级用户不受 CONNECT 限制）
REVOKE CONNECT ON DATABASE memory FROM PUBLIC;
GRANT CONNECT ON DATABASE memory TO memory;
REVOKE CONNECT ON DATABASE auth FROM PUBLIC;
GRANT CONNECT ON DATABASE auth TO auth;
REVOKE CONNECT ON DATABASE conversation FROM PUBLIC;
GRANT CONNECT ON DATABASE conversation TO conversation;
REVOKE CONNECT ON DATABASE community FROM PUBLIC;
GRANT CONNECT ON DATABASE community TO community;
