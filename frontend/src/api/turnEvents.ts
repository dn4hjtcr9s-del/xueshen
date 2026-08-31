// TurnEventStreamClient（方案 §17.5 / Q15 / §18.3）：
// 项目自有 Fetch SSE 客户端，封装 @microsoft/fetch-event-source；
// React 页面与状态组件不得直接依赖第三方 API。
//
// 职责：Authorization Bearer Header、Last-Event-ID、AbortController、
// 401/403、429、断线退避、页面隐藏/恢复、事件 sequence 去重。
import { fetchEventSource } from "@microsoft/fetch-event-source";
import { getAccessToken } from "../auth/tokenStore";
import { resolveMemoryApiUrl } from "./client";
import type { SSEEnvelope } from "../types/conversation";

export type StreamEvent = SSEEnvelope;

export interface TurnEventStreamOptions {
  url: string;
  onEvent: (event: StreamEvent) => void;
  onError?: (error: Error) => void;
  /** 恢复断线：Last-Event-ID（§17.5 #1）。 */
  resumeSequence?: number | null;
  /** 401/403 后调用方重新认证的钩子；返回 true 表示可重连。 */
  onAuthFailure?: () => Promise<boolean>;
}

const BACKOFF_MS = [500, 1000, 2000, 5000];

/**
 * 建立并维持一条 Fetch SSE 流。
 * 返回停止函数：AbortController 中止 + 清理。断线自动退避重连（页面隐藏期间暂停）。
 */
export function startTurnEventStream(options: TurnEventStreamOptions): () => void {
  const controller = new AbortController();
  let lastSequence = options.resumeSequence ?? null;
  let attempt = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;
  // P2（第三轮评审）：终态事件到达后不再重连
  let terminalReceived = false;

  const connect = () => {
    if (stopped || controller.signal.aborted || document.hidden) return;
    const token = getAccessToken();
    const headers: Record<string, string> = {
      Accept: "text/event-stream",
    };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (lastSequence !== null) headers["Last-Event-ID"] = String(lastSequence);

    fetchEventSource(resolveMemoryApiUrl(options.url), {
      method: "GET",
      headers,
      signal: controller.signal,
      onopen: async (response) => {
        if (response.ok) {
          attempt = 0;
          return;
        }
        if (response.status === 401 || response.status === 403) {
          const renewed = await options.onAuthFailure?.();
          if (renewed) {
            // token 已刷新：立即重连
            connect();
            return;
          }
          throw new Error(`SSE 认证失败（HTTP ${response.status}）`);
        }
        if (response.status === 429) {
          // 限流：按退避重连
          scheduleReconnect();
          throw new Error("SSE 限流（429）");
        }
        if (response.status === 410) {
          // EVENT_REPLAY_EXPIRED（R1）：客户端需重新拉取 Turn/Thread 后再订阅
          options.onError?.(new Error("EVENT_REPLAY_EXPIRED"));
          throw new Error("事件流已过期");
        }
        throw new Error(`SSE 连接失败（HTTP ${response.status}）`);
      },
      onmessage: (raw) => {
        if (!raw.data) return;
        try {
          const event = JSON.parse(raw.data) as StreamEvent;
          // 事件按 sequence 去重（§17.5 #4）
          if (lastSequence !== null && event.sequence <= lastSequence) return;
          lastSequence = event.sequence;
          attempt = 0;
          if (event.event_type === "answer.completed" || event.event_type === "turn.failed" || event.event_type === "turn.cancelled") {
            terminalReceived = true;
          }
          options.onEvent(event);
        } catch {
          // 忽略不可解析事件
        }
      },
      onerror: (error) => {
        if (controller.signal.aborted || stopped) throw error;
        scheduleReconnect();
        throw error; // fetch-event-source 要求 throw 才断开
      },
      onclose: () => {
        // P2（第三轮评审）：收到终态事件（answer.completed/failed/cancelled）后
        // 服务端主动结束流，客户端不得再重连（§17.5 #5：断线默认不取消 Graph，
        // 但终态已达 → 无继续订阅意义）。
        if (!stopped && !controller.signal.aborted && !terminalReceived) {
          scheduleReconnect();
        }
      },
    }).catch(() => {
      // 连接错误由 onerror 处理；此处避免未捕获 Promise
    });
  };

  const scheduleReconnect = () => {
    if (stopped || retryTimer !== null || controller.signal.aborted) return;
    const delay = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
    attempt += 1;
    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
    }, delay);
  };

  const onVisibilityChange = () => {
    if (!document.hidden && !stopped && retryTimer === null) {
      connect();
    }
  };
  document.addEventListener("visibilitychange", onVisibilityChange);

  connect();

  return () => {
    stopped = true;
    controller.abort();
    if (retryTimer !== null) clearTimeout(retryTimer);
    document.removeEventListener("visibilitychange", onVisibilityChange);
  };
}
