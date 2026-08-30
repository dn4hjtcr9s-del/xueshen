#!/bin/sh
# 生产五链迁移 wrapper（community-rebuild-plan.md §7.16 冻结语义 / P5 migrate job）。
#
# 冻结规则：
# - 各链独立提交、无跨库事务；任一链失败 → 打印链名到 stderr → 整体 exit 1
#   （compose 中 restart: "no"，memory-api depends_on service_completed_successfully，
#    迁移失败 = 应用不启动；修复后重跑 job，Alembic 幂等跳过已完成版本）；
# - sync-knowledge-graph --apply 仅在全部链 upgrade 成功后执行（memory 链失败自然到不了这里）；
# - STUDY_DOMAIN_ENABLED!=true 时跳过 study 链（MVP 不启用，脚本不建 study 库）；
# - RAG_MIGRATE_DATABASE_URL 未配置时跳过 rag 链；
# - conversation/community 按对应 *_MIGRATE_DATABASE_URL 是否配置决定（我们部署=配置=执行）。
#
# 迁移用各库属主（owner）角色连接串（DDL 权限），不用运行时最小权限角色：
#   MEMORY_MIGRATE_DATABASE_URL / AUTH_MIGRATE_DATABASE_URL /
#   CONVERSATION_MIGRATE_DATABASE_URL / COMMUNITY_MIGRATE_DATABASE_URL /
#   RAG_MIGRATE_DATABASE_URL
set -u

fail() {
    echo "[migrate-all] 失败：$1" >&2
    exit 1
}

# $1=链名 $2=alembic ini $3=环境变量名 $4=连接串
run_chain() {
    name="$1"
    ini="$2"
    var="$3"
    url="$4"
    if [ -z "$url" ]; then
        echo "[migrate-all] 跳过 $name 链（$var 未配置）"
        return 0
    fi
    echo "[migrate-all] 迁移 $name 链（$ini）..."
    # 各链 env.py 读取的环境变量名不同，按链注入（memory 链经 settings 读 DATABASE_URL）
    env "$var=$url" uv run --no-sync alembic -c "$ini" upgrade head \
        || fail "$name 链 upgrade head 非零退出（$ini）"
    echo "[migrate-all] $name 链完成"
}

# 顺序冻结：memory → auth → conversation → community → rag（§7.16 链清单）
run_chain memory alembic.ini DATABASE_URL "${MEMORY_MIGRATE_DATABASE_URL:-}"
run_chain auth auth_alembic.ini AUTH_DATABASE_URL "${AUTH_MIGRATE_DATABASE_URL:-}"
run_chain conversation conversation_alembic.ini CONVERSATION_DATABASE_URL "${CONVERSATION_MIGRATE_DATABASE_URL:-}"
run_chain community community_alembic.ini COMMUNITY_DATABASE_URL "${COMMUNITY_MIGRATE_DATABASE_URL:-}"

if [ "${STUDY_DOMAIN_ENABLED:-false}" = "true" ]; then
    run_chain study study_alembic.ini STUDY_DATABASE_URL "${STUDY_MIGRATE_DATABASE_URL:-}"
else
    echo "[migrate-all] 跳过 study 链（STUDY_DOMAIN_ENABLED!=true）"
fi

run_chain rag rag_alembic.ini RAG_DATABASE_URL "${RAG_MIGRATE_DATABASE_URL:-}"

# 迁移后授权兜底（P5 实测踩坑）：DEFAULT PRIVILEGES 只覆盖"之后"新建的对象，
# 且 schema USAGE 无法预授给迁移前尚不存在的 schema（memory.ops /
# conversation.conversation / rag.rag 等）。每链完成后以 owner 身份对本库
# 全部业务 schema 做幂等授权，保证 app 角色可读维护闸门等运行必需表。
grant_app_access() {
    name="$1"
    url="$2"
    app_role="$3"
    if [ -z "$url" ]; then
        return 0
    fi
    echo "[migrate-all] $name 库授权 $app_role（全业务 schema 幂等）..."
    GRANT_URL="$url" GRANT_ROLE="$app_role" uv run --no-sync python - <<'PY' \
        || fail "$name 库授权非零退出"
import os

import psycopg
from psycopg import sql

url = os.environ["GRANT_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
role = os.environ["GRANT_ROLE"]

# 只授权 current_user（owner）自己拥有的对象：LangGraph 运行时以 app 角色自建的
# checkpoint 表归 app 所有，owner 对非自有对象 GRANT 会报 InsufficientPrivilege
# （P5 实测踩坑）；app 自建表天然全权，无需 owner 再授。
with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
    cur.execute(
        "SELECT schema_name FROM information_schema.schemata "
        "WHERE schema_name !~ '^pg_' AND schema_name <> 'information_schema'"
    )
    schemas = [row[0] for row in cur.fetchall()]
    for schema in schemas:
        cur.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
    cur.execute(
        "SELECT schemaname, tablename FROM pg_tables "
        "WHERE schemaname !~ '^pg_' AND schemaname <> 'information_schema' "
        "AND tableowner = current_user"
    )
    tables = cur.fetchall()
    for schema, table in tables:
        cur.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {}.{} TO {}").format(
                sql.Identifier(schema), sql.Identifier(table), sql.Identifier(role)
            )
        )
    cur.execute(
        "SELECT schemaname, sequencename FROM pg_sequences "
        "WHERE schemaname !~ '^pg_' AND schemaname <> 'information_schema' "
        "AND sequenceowner = current_user"
    )
    for schema, seq in cur.fetchall():
        cur.execute(
            sql.SQL("GRANT USAGE, SELECT ON SEQUENCE {}.{} TO {}").format(
                sql.Identifier(schema), sql.Identifier(seq), sql.Identifier(role)
            )
        )
    print(f"[grant] schemas={schemas} tables={len(tables)} role={role} 完成")
PY
}

grant_app_access memory "${MEMORY_MIGRATE_DATABASE_URL:-}" memory_app
grant_app_access auth "${AUTH_MIGRATE_DATABASE_URL:-}" auth_app
grant_app_access conversation "${CONVERSATION_MIGRATE_DATABASE_URL:-}" conversation_app
grant_app_access community "${COMMUNITY_MIGRATE_DATABASE_URL:-}" community_app
if [ "${STUDY_DOMAIN_ENABLED:-false}" = "true" ]; then
    grant_app_access study "${STUDY_MIGRATE_DATABASE_URL:-}" study_app
fi
grant_app_access rag "${RAG_MIGRATE_DATABASE_URL:-}" rag_app

# LangGraph checkpointer 运行时自建表（saver.setup() 在应用启动时执行 DDL），
# app 角色必须持有对应 schema 的 CREATE 权限；CREATE TABLE IF NOT EXISTS 在表已存在时
# 依然要求 CREATE，无法靠预建表规避（P5 实测踩坑：memory-api/worker 启动即崩）。
# 冻结清单：memory 库 public schema（memory_app）、conversation 库
# conversation_checkpoints schema（conversation_app）。
grant_checkpoint_create() {
    name="$1"
    url="$2"
    schema="$3"
    app_role="$4"
    if [ -z "$url" ]; then
        return 0
    fi
    echo "[migrate-all] $name 库授权 ${app_role} CREATE ON SCHEMA ${schema}（LangGraph 运行时 DDL）..."
    GRANT_URL="$url" GRANT_SCHEMA="$schema" GRANT_ROLE="$app_role" \
        uv run --no-sync python - <<'PY' || fail "$name 库 checkpoint CREATE 授权非零退出"
import os

import psycopg
from psycopg import sql

url = os.environ["GRANT_URL"].replace("postgresql+psycopg://", "postgresql://", 1)
with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
    cur.execute(
        sql.SQL("GRANT CREATE ON SCHEMA {} TO {}").format(
            sql.Identifier(os.environ["GRANT_SCHEMA"]),
            sql.Identifier(os.environ["GRANT_ROLE"]),
        )
    )
print("[grant] checkpoint CREATE 完成")
PY
}

grant_checkpoint_create memory "${MEMORY_MIGRATE_DATABASE_URL:-}" public memory_app
grant_checkpoint_create conversation "${CONVERSATION_MIGRATE_DATABASE_URL:-}" conversation_checkpoints conversation_app

# 全部链成功后同步知识图谱注册表（否则 /health/ready 报 knowledge_graph_registry_not_loaded）。
# sync 写 memory 库，用属主连接串。
if [ -n "${MEMORY_MIGRATE_DATABASE_URL:-}" ]; then
    echo "[migrate-all] 同步知识图谱注册表..."
    env DATABASE_URL="$MEMORY_MIGRATE_DATABASE_URL" \
        uv run --no-sync python -m backend.memory.cli sync-knowledge-graph --apply \
        || fail "sync-knowledge-graph --apply 非零退出"
fi

echo "[migrate-all] 全部完成"
exit 0
