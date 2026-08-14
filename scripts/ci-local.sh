#!/usr/bin/env bash
# 本地统一 CI 入口（规格 §23.7）。本期不创建 GitHub Actions workflow。
# 用法: scripts/ci-local.sh [stage ...]
# 无参数时按顺序执行全部 stage。
set -euo pipefail

cd "$(dirname "$0")/.."

STAGES=(backend-lint backend-unit backend-integration frontend contracts container-build)

run_backend_lint() {
  echo "== backend-lint: Ruff + mypy =="
  # 检查范围明确为 backend/ 与 tests/（复审 nit）：scripts/mineru_ocr/ 等
  # 遗留工具目录不在门禁范围内（44 处存量错误，另行治理）
  uv run ruff check backend tests
  uv run ruff format --check backend tests
  uv run mypy backend
}

run_backend_unit() {
  echo "== backend-unit: pytest unit（+ graph 目录存在时）与现有 OCR 测试 =="
  uv sync --extra dev --extra ocr
  GRAPH_TESTS=()
  if [[ -d tests/graph ]]; then
    GRAPH_TESTS=(tests/graph)
  fi
  uv run pytest tests/unit ${GRAPH_TESTS[@]+"${GRAPH_TESTS[@]}"} tests/test_mineru_ocr_client.py \
    tests/test_mineru_ocr_manifest.py tests/test_mineru_ocr_merge.py \
    tests/test_mineru_ocr_runner.py tests/test_run_mineru_ocr_cli.py
}

run_backend_integration() {
  echo "== backend-integration: 本地 PostgreSQL 容器 + memory/auth/conversation/community_test 独立测试库 =="
  # 干净环境首次启动时必须等待 initdb 与 healthcheck 完成，避免迁移抢跑。
  docker compose up -d --wait postgres
  # 测试库隔离（附录 A.6 #20 / 评审 P1-1）：管理员创建、迁移后经环境变量注入，
  # 绝不触碰开发库 memory / auth / conversation / community
  local admin="${POSTGRES_ADMIN_USER:-postgres}"
  ensure_test_database() {
    local db="$1" owner="$2"
    if ! docker compose exec -T postgres psql -U "$admin" -tAc \
      "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1; then
      docker compose exec -T postgres createdb -U "$admin" -O "$owner" "$db"
    fi
  }
  ensure_test_database memory_test memory
  ensure_test_database auth_test auth
  ensure_test_database conversation_test conversation
  ensure_test_database community_test community
  DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test" \
    uv run alembic upgrade head
  AUTH_DATABASE_URL="postgresql+psycopg://auth:auth@127.0.0.1:55432/auth_test" \
    uv run alembic -c auth_alembic.ini upgrade head
  CONVERSATION_DATABASE_URL="postgresql+psycopg://conversation:conversation@127.0.0.1:55432/conversation_test" \
    uv run alembic -c conversation_alembic.ini upgrade head
  COMMUNITY_DATABASE_URL="postgresql+psycopg://community:community@127.0.0.1:55432/community_test" \
    uv run alembic -c community_alembic.ini upgrade head
  # Graph 集成测试依赖只读注册表；迁移只建表，需按启动契约显式同步权威图谱。
  DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test" \
    uv run python -m backend.memory.cli sync-knowledge-graph --apply
  FAILURE_TESTS=()
  if [[ -d tests/failure_recovery ]]; then
    FAILURE_TESTS=(tests/failure_recovery)
  fi
  CONVERSION_TESTS=()
  if [[ -d tests/conversation ]]; then
    CONVERSION_TESTS=(tests/conversation)
  fi
  COMMUNITY_TESTS=()
  if [[ -d tests/community ]]; then
    COMMUNITY_TESTS=(tests/community)
  fi
  DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test" \
  AUTH_DATABASE_URL="postgresql+psycopg://auth:auth@127.0.0.1:55432/auth_test" \
  CONVERSATION_DATABASE_URL="postgresql+psycopg://conversation:conversation@127.0.0.1:55432/conversation_test" \
  COMMUNITY_DATABASE_URL="postgresql+psycopg://community:community@127.0.0.1:55432/community_test" \
    uv run pytest tests/integration ${FAILURE_TESTS[@]+"${FAILURE_TESTS[@]}"} ${CONVERSION_TESTS[@]+"${CONVERSION_TESTS[@]}"} ${COMMUNITY_TESTS[@]+"${COMMUNITY_TESTS[@]}"}
}

run_frontend() {
  echo "== frontend: npm ci + lint/test/build =="
  (cd frontend && npm ci && npm run lint && npm run test && npm run build)
}

run_contracts() {
  echo "== contracts: OpenAPI snapshot =="
  uv run pytest tests/contract
}

run_container_build() {
  echo "== container-build: docker compose build =="
  docker compose build
}

selected=("$@")
if [[ ${#selected[@]} -eq 0 ]]; then
  selected=("${STAGES[@]}")
fi

for stage in "${selected[@]}"; do
  case "$stage" in
    backend-lint) run_backend_lint ;;
    backend-unit) run_backend_unit ;;
    backend-integration) run_backend_integration ;;
    frontend) run_frontend ;;
    contracts) run_contracts ;;
    container-build) run_container_build ;;
    *) echo "未知 stage: $stage" >&2; exit 2 ;;
  esac
done

echo "本地 CI 全部通过: ${selected[*]}"
