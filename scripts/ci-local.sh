#!/usr/bin/env bash
# 本地统一 CI 入口（规格 §23.7）。本期不创建 GitHub Actions workflow。
# 用法: scripts/ci-local.sh [stage ...]
# 无参数时按顺序执行全部 stage。
set -euo pipefail

cd "$(dirname "$0")/.."

STAGES=(backend-lint backend-unit backend-integration frontend contracts container-build)

run_backend_lint() {
  echo "== backend-lint: Ruff + mypy =="
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
  echo "== backend-integration: 本地 PostgreSQL 容器 + memory_test/auth_test 独立测试库 =="
  docker compose up -d postgres
  # 测试库隔离（附录 A.6 #20 / 评审 P1-1）：管理员创建、迁移后经环境变量注入，
  # 绝不触碰开发库 memory / auth
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
  DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test" \
    uv run alembic upgrade head
  AUTH_DATABASE_URL="postgresql+psycopg://auth:auth@127.0.0.1:55432/auth_test" \
    uv run alembic -c auth_alembic.ini upgrade head
  FAILURE_TESTS=()
  if [[ -d tests/failure_recovery ]]; then
    FAILURE_TESTS=(tests/failure_recovery)
  fi
  DATABASE_URL="postgresql+psycopg://memory:memory@127.0.0.1:55432/memory_test" \
  AUTH_DATABASE_URL="postgresql+psycopg://auth:auth@127.0.0.1:55432/auth_test" \
    uv run pytest tests/integration ${FAILURE_TESTS[@]+"${FAILURE_TESTS[@]}"}
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
