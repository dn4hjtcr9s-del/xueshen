import { defineConfig } from "vitest/config";
import { loadEnv } from "vite";
import react from "@vitejs/plugin-react";

// §20.4：本地开发通过 Vite proxy 注入 Dev Auth（X-Dev-User-Id），
// 该逻辑只存在于开发服务器配置中，不进入 production 构建产物。
// 前端代码不得硬编码用户 ID，也不得发送 X-Dev-Actor-Type / X-Dev-Scopes。
//
// 方案 §9.4：/memory-api 供 memory 业务接口（带 Dev Auth 注入）；
// /api/v1 直连转发供 auth 端点（Cookie Path=/api/v1/auth 必须与浏览器 URL
// 前缀一致，方案 §7）。生产/Docker 同源反向代理，二者合一。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.MEMORY_DEV_API_TARGET ?? "http://localhost:8000";
  const devUserId = env.MEMORY_DEV_USER_ID ?? "";

  const authProxy = {
    "/api/v1": {
      target,
      changeOrigin: true,
    },
  };
  const memoryProxy = {
    "/memory-api": {
      target,
      changeOrigin: true,
      rewrite: (path: string) => path.replace(/^\/memory-api/, ""),
      configure: (proxy: {
        on: (event: string, cb: (req: { setHeader: (name: string, value: string) => void }) => void) => void;
      }) => {
        proxy.on("proxyReq", (proxyReq) => {
          if (devUserId) {
            proxyReq.setHeader("X-Dev-User-Id", devUserId);
          }
        });
      },
    },
  };

  return {
    plugins: [react()],
    server: {
      proxy: { ...authProxy, ...memoryProxy },
    },
    // 方案 §9.4 过渡期：vite preview 同样代理（4173 端口浏览器验收）
    preview: {
      proxy: { ...authProxy, ...memoryProxy },
    },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      css: false,
    },
  };
});
