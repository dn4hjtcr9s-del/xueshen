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
