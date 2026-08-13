// 共享请求层（方案 §9.1）：挂 Bearer、带 credentials、统一 401 → single-flight refresh。
// 所有 API 客户端（memory / auth）都经由本层发出请求。
import { getAccessToken, setAccessToken } from "../auth/tokenStore";

const API_BASE: string = import.meta.env.VITE_MEMORY_API_BASE_URL ?? "/memory-api";
const V1 = `${API_BASE}/api/v1`;
// auth 端点固定走 /api/v1 直连路径（与生产同源部署一致，方案 §6.4/§7）：
// refresh Cookie Path=/api/v1/auth 必须与浏览器 URL 前缀一致，否则浏览器不会回传。
const AUTH_V1 = "/api/v1";

export interface PublicError {
  code: string;
  message: string;
  retryable: boolean;
  field: string | null;
  trace_id: string;
}

export class MemoryApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly field: string | null;
  readonly traceId: string | null;

  constructor(status: number, error: Partial<PublicError> | undefined, fallback: string) {
    super(error?.message ?? fallback);
    this.status = status;
    this.code = error?.code ?? "INTERNAL_ERROR";
    this.retryable = error?.retryable ?? false;
    this.field = error?.field ?? null;
    this.traceId = error?.trace_id ?? null;
  }
}

export async function errorFromResponse(response: Response, fallback: string): Promise<MemoryApiError> {
  let error: Partial<PublicError> | undefined;
  try {
    error = (await response.json()).error;
  } catch {
    // 非 JSON 错误体：用 HTTP 状态兜底
  }
  return new MemoryApiError(response.status, error, fallback);
}

// 全局 single-flight refresh（方案 §9.3）：所有并发 401 共享同一个刷新 Promise，
// 杜绝"第一个 refresh 轮换后第二个被误判重放导致整族撤销"。
let refreshPromise: Promise<boolean> | null = null;

// 会话彻底失效（refresh 失败）时的回调：由 AuthContext 注册，跳回登录页。
let sessionExpiredHandler: (() => void) | null = null;

export function setSessionExpiredHandler(handler: (() => void) | null): void {
  sessionExpiredHandler = handler;
}

async function refreshSession(): Promise<boolean> {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const response = await fetch(`${AUTH_V1}/auth/refresh`, {
          method: "POST",
          credentials: "include",
        });
        if (!response.ok) {
          setAccessToken(null);
          sessionExpiredHandler?.();
          return false;
        }
        const data = (await response.json()) as { access_token?: string };
        setAccessToken(data.access_token ?? null);
        return Boolean(data.access_token);
      } catch {
        setAccessToken(null);
        sessionExpiredHandler?.();
        return false;
      } finally {
        refreshPromise = null;
      }
    })();
  }
  return refreshPromise;
}

/** 供 AuthContext 启动时恢复会话（single-flight 与 401 流程共享同一实现）。 */
export async function restoreSessionWithRefresh(): Promise<string | null> {
  if (await refreshSession()) {
    return getAccessToken();
  }
  return null;
}

export interface RequestOptions {
  body?: unknown;
  idempotencyKey?: string;
  query?: Record<string, string>;
  /** auth 端点自身（login/refresh 等）不参与 401 自动刷新，避免递归 */
  auth?: boolean;
}

async function rawFetch(method: string, path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = {};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  const token = getAccessToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  const base = path.startsWith("/auth/") ? AUTH_V1 : V1;
  const url = options.query
    ? `${base}${path}?${new URLSearchParams(options.query).toString()}`
    : `${base}${path}`;
  return fetch(url, {
    method,
    headers,
    credentials: "include",
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });
}

export async function request<T>(
  method: string,
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  let response = await rawFetch(method, path, options);
  // 统一 401 → single-flight refresh → 重放一次（auth 端点自身除外）
  if (response.status === 401 && !options.auth) {
    if (await refreshSession()) {
      response = await rawFetch(method, path, options);
    }
  }
  if (!response.ok) {
    throw await errorFromResponse(response, `请求失败（HTTP ${response.status}）`);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export function idempotencyKey(): string {
  return crypto.randomUUID();
}
