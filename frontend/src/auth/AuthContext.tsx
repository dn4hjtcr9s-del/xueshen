// 登录态上下文（方案 §9.1）：顶层 AuthProvider + AuthGate。
// 未登录时直接渲染登录/注册页（即"跳转登录页"的实现方式，不引入路由库）。
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
import { restoreSessionWithRefresh, setSessionExpiredHandler } from "../api/client";

interface AuthContextValue {
  user: AuthUser | null;
  /** 是否完成启动时的静默会话恢复（避免闪跳登录页） */
  ready: boolean;
  login: (identifier: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** 当前用户首字（顶部用户标识，方案 §9.2） */
  initials: string;
}

const AuthContext = createContext<AuthContextValue | null>(null);

async function restoreSession(): Promise<AuthUser | null> {
  // 页面刷新后 access token 为空：先静默 refresh 恢复，再 /me 取用户信息
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
    setUser(data.user);
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiLogout();
    } finally {
      setUser(null);
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      ready,
      login,
      logout,
      initials: user ? user.username.slice(0, 1).toUpperCase() : "",
    }),
    [user, ready, login, logout],
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
