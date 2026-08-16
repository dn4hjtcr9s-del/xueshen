// 登录态上下文（方案 §9.1）：顶层 AuthProvider。
// 访客（未登录）直接浏览主应用；登录/注册从「个人中心」进入（ProfilePage 未登录态）。
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { login as apiLogin, logout as apiLogout, me as apiMe, type AuthUser } from "../api/auth";
import {
  notifyLogout,
  restoreSessionWithRefresh,
  setSessionExpiredHandler,
} from "../api/client";

interface AuthContextValue {
  user: AuthUser | null;
  /** 是否完成启动时的静默会话恢复（避免闪跳） */
  ready: boolean;
  login: (identifier: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** 服务端退出失败时的用户提示（评审 P1-3：凭据可能残留，需明确告知） */
  logoutWarning: string | null;
  /** 当前用户首字（顶部用户标识，方案 §9.2） */
  initials: string;
}

const AuthContext = createContext<AuthContextValue | null>(null);

async function restoreSession(): Promise<AuthUser | null> {
  // 页面刷新后 access token 为空：先静默 refresh 恢复，再 /me 取用户信息。
  // 未登录（访客）也直接进入主应用（main.tsx 不设门禁），这里只决定
  // 是否展示已登录用户信息，失败时保持匿名浏览。
  try {
    const token = await restoreSessionWithRefresh();
    if (!token) return null;
    return await apiMe();
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [ready, setReady] = useState(false);
  const [logoutWarning, setLogoutWarning] = useState<string | null>(null);

  // 会话彻底失效（任意请求 refresh 失败）→ 回到登录页
  useEffect(() => {
    setSessionExpiredHandler(() => setUser(null));
    return () => setSessionExpiredHandler(null);
  }, []);

  useEffect(() => {
    let cancelled = false;
    void restoreSession().then((restored) => {
      if (!cancelled) {
        setUser(restored);
        setReady(true);
      }
    });
    return () => {
      cancelled = true;
    };
  }, []);

  const login = useCallback(async (identifier: string, password: string) => {
    const data = await apiLogin(identifier, password);
    setLogoutWarning(null);
    setUser(data.user);
  }, []);

  // 复审 P1-3：服务端 logout 失败时本地凭据已由 apiLogout 的 finally 清除，
  // 但 HttpOnly refresh Cookie 无法由前端删除、服务器会话可能仍有效——
  // 必须明确告知用户，而不是无提示地显示"已退出"。
  const logout = useCallback(async () => {
    try {
      await apiLogout();
      setLogoutWarning(null);
    } catch {
      setLogoutWarning(
        "服务端退出失败：本机登录状态已清除，但服务器会话可能仍有效。若在使用共用设备，请稍后重试退出。",
      );
    } finally {
      notifyLogout();
      setUser(null);
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      ready,
      login,
      logout,
      logoutWarning,
      initials: user ? user.username.slice(0, 1).toUpperCase() : "",
    }),
    [user, ready, login, logout, logoutWarning],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth 必须在 AuthProvider 内使用");
  }
  return value;
}
