// Playwright 浏览器主链路 E2E 配置（方案 §12 验收 / 附录 A.6 #19）。
// 后端由 scripts/e2e-auth.sh 以 DEV_AUTH_ENABLED=false 启动（8002），
// 前端 vite preview（4173）代理 /api/v1 与 /memory-api → 8002。
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  retries: 1,
  workers: 1,
  use: {
    baseURL: "http://localhost:4173",
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "npm run preview -- --port 4173",
    url: "http://localhost:4173",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
