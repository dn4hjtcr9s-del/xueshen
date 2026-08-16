import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "katex/dist/katex.min.css";
import "./styles.css";
import App from "./App";
import { AuthProvider } from "./auth/AuthContext";

// 访客可浏览：不再用 AuthGate 拦截未登录用户，直接渲染主应用；
// 登录/注册为可选项，从「个人中心」进入（ProfilePage 未登录态）。
createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthProvider>
      <App />
    </AuthProvider>
  </StrictMode>
);
