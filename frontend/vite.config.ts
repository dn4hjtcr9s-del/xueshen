import { defineConfig } from "vitest/config";
import { loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// §20.4：本地开发通过 Vite proxy 注入 Dev Auth（X-Dev-User-Id），
// 该逻辑只存在于开发服务器配置中，不进入 production 构建产物。
// 前端代码不得硬编码用户 ID，也不得发送 X-Dev-Actor-Type / X-Dev-Scopes。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.MEMORY_DEV_API_TARGET ?? "http://localhost:8000";
  const devUserId = env.MEMORY_DEV_USER_ID ?? "";
  return {
    plugins: [react()],
    server: {
      proxy: {
        "/memory-api": {
          target,
          changeOrigin: true,
          rewrite: (path) => path.replace(/^\/memory-api/, ""),
          configure: (proxy) => {
            proxy.on("proxyReq", (proxyReq) => {
              if (devUserId) {
                proxyReq.setHeader("X-Dev-User-Id", devUserId);
              }
            });
          },
        },
      },
    },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      css: false,
    },
  };
});
