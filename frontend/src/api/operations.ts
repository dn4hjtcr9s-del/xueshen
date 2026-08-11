// Operation 轮询（规格 §20.3）。
// 初始 500ms，随后 1s、2s 指数退避，最大 5s；terminal 状态停止；
// 页面隐藏后降到 15s，恢复可见立即查询；最长等待 2 分钟，
// 超时只提示“任务仍在后台处理”，不取消服务器任务。
import { useEffect, useRef, useState } from "react";
import { getOperation, type MemoryOperationResult, type OperationStatus } from "./memory";

export const POLL_FIRST_DELAY_MS = 500;
export const POLL_MAX_DELAY_MS = 5000;
export const POLL_HIDDEN_DELAY_MS = 15000;
export const POLL_TIMEOUT_MS = 120_000;

const TERMINAL_STATUSES: ReadonlySet<OperationStatus> = new Set([
  "succeeded",
  "needs_review",
  "dead_letter",
  "cancelled",
]);

export function isTerminalStatus(status: OperationStatus): boolean {
  return TERMINAL_STATUSES.has(status);
}

/** 第 attempt 次（从 0 起）轮询后的等待时长：500 → 1000 → 2000 → 4000 → 5000（封顶）。 */
export function nextPollDelay(attempt: number, hidden: boolean): number {
  if (hidden) return POLL_HIDDEN_DELAY_MS;
  return Math.min(POLL_FIRST_DELAY_MS * 2 ** attempt, POLL_MAX_DELAY_MS);
}

export interface OperationPollingState {
  result: MemoryOperationResult | null;
  /** 仍在等待（未 terminal 且未超时） */
  pending: boolean;
  /** 超过 2 分钟未 terminal：提示任务仍在后台处理（不取消服务器任务） */
  timedOut: boolean;
}

export function useOperationPolling(operationId: string | null): OperationPollingState {
  const [result, setResult] = useState<MemoryOperationResult | null>(null);
  const [timedOut, setTimedOut] = useState(false);
  const attemptRef = useRef(0);

  useEffect(() => {
    if (!operationId) {
      setResult(null);
      setTimedOut(false);
      return;
    }
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const startedAt = Date.now();
    attemptRef.current = 0;
    setResult(null);
    setTimedOut(false);

    const tick = async () => {
      if (cancelled) return;
      let terminal = false;
      try {
        const operation = await getOperation(operationId);
        if (cancelled) return;
        setResult(operation);
        terminal = isTerminalStatus(operation.status);
      } catch {
        // 查询失败不中断轮询；写操作已入队，由指数退避兜底
      }
      if (terminal || cancelled) return;
      if (Date.now() - startedAt >= POLL_TIMEOUT_MS) {
        setTimedOut(true);
        return;
      }
      const delay = nextPollDelay(attemptRef.current, document.hidden);
      attemptRef.current += 1;
      timer = setTimeout(tick, delay);
    };

    const onVisibilityChange = () => {
      // 恢复可见立即查询一次
      if (!document.hidden && !cancelled) {
        if (timer) clearTimeout(timer);
        void tick();
      }
    };

    timer = setTimeout(tick, nextPollDelay(0, document.hidden));
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [operationId]);

  const pending =
    operationId !== null && !timedOut && (result === null || !isTerminalStatus(result.status));
  return { result, pending, timedOut };
}
