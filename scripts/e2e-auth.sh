#!/usr/bin/env bash
# 浏览器主链路 E2E 入口（方案 §12 验收 / 附录 A.6 #19）：
# 注册 → 登录 → 刷新页面会话不丢 → 退出登录后访问被拒。
#
# 后端以 DEV_AUTH_ENABLED=false 独立启动（8002，排除 Dev Auth 绕过），
# 前端 vite preview（4173）代理 /api/v1 与 /memory-api → 8002（方案 §9.4 过渡期）。
# 前置：docker compose postgres 运行中、本地密钥已生成（scripts/generate_auth_keys.sh）。
#
# 用法：bash scripts/e2e-auth.sh
set -euo pipefail
cd "$(dirname "$0")/.."

API_PID=""
cleanup() {
  if [[ -n "$API_PID" ]]; then
    kill "$API_PID" 2>/dev/null || true
    wait "$API_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

echo "== 启动后端（DEV_AUTH_ENABLED=false, :8002） =="
env \
  APP_ENV=development \
  DEV_AUTH_ENABLED=false \
  AUTH_DATABASE_URL="postgresql+psycopg://auth:auth@127.0.0.1:55432/auth" \
  AUTH_ISSUER=gewu-auth \
  AUTH_PRIVATE_KEY_FILE="$PWD/.local/keys/auth_private.pem" \
  AUTH_PUBLIC_KEY_FILE="$PWD/.local/keys/auth_public.pem" \
  MEMORY_STORAGE_ROOT="$PWD/.local/e2e-memory" \
  OPENAI_API_KEY="e2e-placeholder" \
  uv run uvicorn backend.app:app --host 127.0.0.1 --port 8002 &
API_PID=$!

for _ in $(seq 1 90); do
  if curl -sf http://127.0.0.1:8002/health/ready >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "后端启动失败" >&2
    exit 1
  fi
  sleep 1
done
if ! curl -sf http://127.0.0.1:8002/health/ready >/dev/null 2>&1; then
  echo "后端就绪超时" >&2
  exit 1
fi

echo "== 前端构建 + Playwright E2E =="
(cd frontend && npm run build >/dev/null)
(cd frontend && MEMORY_DEV_API_TARGET=http://localhost:8002 npx playwright test --config playwright.config.ts)
