// 共享请求层（方案 §9.1）：挂 Bearer、带 credentials、统一 401 → single-flight refresh。
// 所有 API 客户端（memory / auth）都经由本层发出请求。
import {
  adoptLogoutEpoch,
  getAccessToken,
  getAccessTokenGeneration,
  getLogoutEpoch,
  incrementLogoutEpoch,
  setAccessToken,
} from "../auth/tokenStore";

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
  // 附录 A.4：仅 THREAD_VERSION_CONFLICT 时出现
  current_version?: number | null;
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

// 复审 P1-2 / P1：单页 refreshPromise 只能协调同一 JS context。多个浏览器标签页
// 共享 HttpOnly refresh Cookie 但各自有独立 context，必须用 Web Locks 串行化跨
// 标签页 refresh（等待期间 Cookie 罐已被其他标签页轮换，不会提交旧 token 触发
// 重放），并用 BroadcastChannel 同步新 token / 退出事件。
const REFRESH_LOCK_NAME = "gewu-auth-refresh";
const SESSION_CHANNEL_NAME = "gewu-auth-session";

// 复审 P1：频道在模块加载时立即初始化（而非首次使用时惰性创建）——
// 等待锁的标签页若在 broadcast 前尚未订阅，会错过 logout 消息；
// epoch 标记（localStorage）兜底保证即使错过消息也不会恢复已退出会话。
const sessionChannel: BroadcastChannel | null =
  typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel(SESSION_CHANNEL_NAME);

if (sessionChannel !== null) {
  sessionChannel.addEventListener("message", (event: MessageEvent) => {
    const message = event.data as { type?: string; token?: string; epoch?: number } | null;
    if (message?.type === "access-token" && typeof message.token === "string") {
      // 复审 P1：仅同会话世代（发送方 epoch 与本地一致）时采用新 token，
      // 杜绝"已退出标签页收到旧广播重新携带 Bearer"
      if (message.epoch === getLogoutEpoch()) {
        setAccessToken(message.token);
      }
    } else if (message?.type === "session-expired" || message?.type === "logout") {
      setAccessToken(null);
      // 复审 P1：接收端只取 max（消息携带发送方 epoch），绝不自行递增，
      // 避免新标签页把全局 epoch 回退
      adoptLogoutEpoch(message.epoch ?? 0);
      sessionExpiredHandler?.();
    }
  });
}

function broadcast(message: { type: string; token?: string; epoch?: number }): void {
  sessionChannel?.postMessage(message);
}

/** 退出登录后通知其他标签页同步清除本地会话（epoch 已在锁内递增）。 */
export function notifyLogout(): void {
  broadcast({ type: "logout", epoch: getLogoutEpoch() });
}

// 会话彻底失效（refresh 失败）时的回调：由 AuthContext 注册，跳回登录页。
let sessionExpiredHandler: (() => void) | null = null;

export function setSessionExpiredHandler(handler: (() => void) | null): void {
  sessionExpiredHandler = handler;
}

async function doRefreshRequest(): Promise<boolean> {
  const epochBeforeRequest = getLogoutEpoch();
  let response: Response;
  try {
    response = await fetch(`${AUTH_V1}/auth/refresh`, {
      method: "POST",
      credentials: "include",
    });
  } catch {
    // 复审 P2：网络/网关故障是可重试故障，不清 token、不广播会话失效
    return false;
  }
  if (response.status === 401) {
    // 仅 401 AUTH_SESSION_INVALID 才视为凭据失效
    setAccessToken(null);
    incrementLogoutEpoch();
    broadcast({ type: "session-expired", epoch: getLogoutEpoch() });
    sessionExpiredHandler?.();
    return false;
  }
  if (!response.ok) {
    // 复审 P2：429 / 5xx 是临时故障，保留现有会话，稍后自然重试
    return false;
  }
  // 复审 P1：请求期间若收到其他标签页的 logout/session-expired（epoch 变化），
  // 丢弃本次响应，不得写 token、不得广播新 access token
  if (getLogoutEpoch() !== epochBeforeRequest) {
    setAccessToken(null);
    return false;
  }
  const data = (await response.json()) as { access_token?: string };
  setAccessToken(data.access_token ?? null);
  broadcast({
    type: "access-token",
    token: data.access_token,
    epoch: getLogoutEpoch(),
  });
  return Boolean(data.access_token);
}

// 复审 P1：无 Web Locks 环境下的同页面互斥队列——logout 与 refresh 串行执行，
// 消除"refresh 在途时 logout 完成、响应后又写回 token"的竞态
let pageMutex: Promise<void> = Promise.resolve();

/** 复审 P1：logout 与 refresh 使用同一把跨标签页锁（Web Locks）；
 *  无 Web Locks 时退化为同页面互斥队列。 */
export async function withRefreshLock<T>(task: () => Promise<T>): Promise<T> {
  if (typeof navigator !== "undefined" && navigator.locks?.request) {
    return await navigator.locks.request(REFRESH_LOCK_NAME, task);
  }
  const previous = pageMutex;
  let release!: () => void;
  pageMutex = new Promise<void>((resolve) => {
    release = resolve;
  });
  await previous;
  try {
    return await task();
  } finally {
    release();
  }
}

async function refreshSession(): Promise<boolean> {
  if (!refreshPromise) {
    refreshPromise = (async () => {
      try {
        const generationBefore = getAccessTokenGeneration();
        const epochBefore = getLogoutEpoch();
        // 复审 P1：等锁期间必须复查 logout epoch——任何标签页在本标签页排队
        // 期间发生 logout（本地退出或 401 失效），本标签页不得发起 refresh、
        // 不得广播新 access token，直接按会话失效处理
        const refreshOrReuse = async (): Promise<boolean> => {
          if (getLogoutEpoch() !== epochBefore) {
            setAccessToken(null);
            return false;
          }
          // 复审 P2：等锁期间若其他标签页已广播新 token（或已失效），
          // 直接复用该结果，不再发起一次轮换（避免连续换 Cookie 触发限流）
          if (getAccessTokenGeneration() !== generationBefore) {
            return getAccessToken() !== null;
          }
          return await doRefreshRequest();
        };
        // 复审 P3：统一经 withRefreshLock——Web Locks 可用时用跨标签页锁，
        // 不可用时退化为同页互斥队列（logout 与 refresh 同队列串行），
        // 消除"回退路径绕过互斥"导致同页并发轮换/重放的问题
        return await withRefreshLock(refreshOrReuse);
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
  /** multipart 上传（与 body 互斥）：不设置 Content-Type，由浏览器生成 boundary */
  formData?: FormData;
  /** 上传取消（AbortController，§九 D24） */
  signal?: AbortSignal;
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
    body: options.formData ?? (options.body !== undefined ? JSON.stringify(options.body) : undefined),
    signal: options.signal,
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
