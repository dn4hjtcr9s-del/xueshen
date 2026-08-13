// AuthGate（方案 §9.1）：未登录时渲染登录/注册页，登录后渲染主应用。
import { useState, type ReactNode } from "react";
import { useAuth } from "./AuthContext";
import { LoginPage } from "../pages/Login";
import { RegisterPage } from "../pages/Register";

export function AuthGate({ children }: { children: ReactNode }) {
  const { user, ready } = useAuth();
  const [mode, setMode] = useState<"login" | "register">("login");

  if (!ready) {
    // 启动静默会话恢复中：避免闪跳登录页
    return null;
  }
  if (user === null) {
    return mode === "login" ? (
      <LoginPage onGoRegister={() => setMode("register")} />
    ) : (
      <RegisterPage onGoLogin={() => setMode("login")} />
    );
  }
  return <>{children}</>;
}
