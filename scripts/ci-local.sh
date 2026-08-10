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
  echo "== backend-unit: pytest unit/graph + 现有 OCR 测试 =="
  uv sync --extra dev --extra ocr
  uv run pytest tests/unit tests/graph tests/test_mineru_ocr_client.py \
    tests/test_mineru_ocr_manifest.py tests/test_mineru_ocr_merge.py \
    tests/test_mineru_ocr_runner.py tests/test_run_mineru_ocr_cli.py
}

run_backend_integration() {
  echo "== backend-integration: 本地 PostgreSQL 容器 + integration/failure tests =="
  docker compose up -d postgres
  uv run alembic upgrade head
  uv run pytest tests/integration tests/failure_recovery
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
