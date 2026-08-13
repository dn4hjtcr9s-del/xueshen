// 认证 API 客户端（方案 §4.1 / §9.1）。
import { request, withRefreshLock } from "./client";
import { getAccessToken, incrementLogoutEpoch, setAccessToken } from "../auth/tokenStore";

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
  // 复审 P1：logout 与 refresh 共用同一把跨标签页锁。epoch 递增必须在
  // logout **结束时**（锁内、请求之后）完成——任何在 logout 进行中排队等待
  // 锁的 refresh，其快照必然早于该递增，拿到锁后看到 epoch 变化即中止，
  // 不会请求 refresh 也不会广播新 access token。
  // 无论服务端成败，本地 access token 必须清除（本地退出语义，评审 P1-3），
  // 失败时异常照常抛出，由调用方提示用户服务器会话可能仍有效。
  try {
    await withRefreshLock(async () => {
      try {
        await request<void>("POST", "/auth/logout", { auth: true });
      } finally {
        incrementLogoutEpoch();
        setAccessToken(null);
      }
    });
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
