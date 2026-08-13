// 认证 API 客户端（方案 §4.1 / §9.1）。
import { request } from "./client";
import { getAccessToken, setAccessToken } from "../auth/tokenStore";

export interface AuthUser {
  user_id: string;
  username: string;
  email: string | null;
  status: "active" | "disabled";
  created_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  user: AuthUser;
}

export async function register(
  username: string,
  password: string,
  email?: string,
): Promise<{ user: AuthUser }> {
  return request("POST", "/auth/register", {
    body: { username, email: email ?? null, password },
    auth: true,
  });
}

export async function login(identifier: string, password: string): Promise<TokenResponse> {
  const data = await request<TokenResponse>("POST", "/auth/login", {
    body: { identifier, password },
    auth: true,
  });
  setAccessToken(data.access_token);
  return data;
}

export async function logout(): Promise<void> {
  // 复审 P1-3：无论服务端成败，本地 access token 必须清除（本地退出语义），
  // 失败时异常照常抛出，由调用方提示用户服务器会话可能仍有效
  try {
    await request<void>("POST", "/auth/logout", { auth: true });
  } finally {
    setAccessToken(null);
  }
}

export async function me(): Promise<AuthUser> {
  // /me 需要 Bearer；未登录时直接抛 401，由 AuthContext 走静默 refresh 恢复
  const token = getAccessToken();
  if (!token) {
    throw new Error("AUTH_REQUIRED");
  }
  return request<AuthUser>("GET", "/auth/me", { auth: true });
}
