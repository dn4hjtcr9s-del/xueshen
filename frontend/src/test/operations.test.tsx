// Operation 轮询测试（§20.3 / §23.6）：退避序列、terminal 停止、2 分钟超时。
import { renderHook, act } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  isTerminalStatus,
  nextPollDelay,
  POLL_HIDDEN_DELAY_MS,
  POLL_TIMEOUT_MS,
  useOperationPolling,
} from "../api/operations";
import { operationResult } from "./fixtures";
import { server } from "./server";

describe("nextPollDelay（§20.3：500ms → 1s → 2s → 最大 5s）", () => {
  it("指数退避并封顶 5s", () => {
    expect(nextPollDelay(0, false)).toBe(500);
    expect(nextPollDelay(1, false)).toBe(1000);
    expect(nextPollDelay(2, false)).toBe(2000);
    expect(nextPollDelay(3, false)).toBe(4000);
    expect(nextPollDelay(4, false)).toBe(5000);
    expect(nextPollDelay(9, false)).toBe(5000);
  });

  it("页面隐藏后固定 15s", () => {
    expect(nextPollDelay(0, true)).toBe(POLL_HIDDEN_DELAY_MS);
    expect(nextPollDelay(5, true)).toBe(POLL_HIDDEN_DELAY_MS);
  });
});

describe("isTerminalStatus", () => {
  it("terminal 集合", () => {
    expect(isTerminalStatus("succeeded")).toBe(true);
    expect(isTerminalStatus("needs_review")).toBe(true);
    expect(isTerminalStatus("dead_letter")).toBe(true);
    expect(isTerminalStatus("cancelled")).toBe(true);
    expect(isTerminalStatus("queued")).toBe(false);
    expect(isTerminalStatus("running")).toBe(false);
    expect(isTerminalStatus("retry_wait")).toBe(false);
  });
});

describe("useOperationPolling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("terminal 状态后停止轮询", async () => {
    let calls = 0;
    server.use(
      http.get("*/api/v1/memory/operations/:id", () => {
        calls += 1;
        return HttpResponse.json(
          calls < 2
            ? operationResult({ status: "running", completed_at: null })
            : operationResult({ status: "succeeded" }),
        );
      }),
    );
    const { result } = renderHook(() => useOperationPolling("op-1"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    expect(calls).toBe(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    expect(calls).toBe(2);
    expect(result.current.result?.status).toBe("succeeded");
    expect(result.current.pending).toBe(false);
    // terminal 后不再发出请求
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30000);
    });
    expect(calls).toBe(2);
  });

  it("超过 2 分钟未 terminal 则提示超时且不取消任务", async () => {
    server.use(
      http.get("*/api/v1/memory/operations/:id", () =>
        HttpResponse.json(operationResult({ status: "running", completed_at: null })),
      ),
    );
    const { result } = renderHook(() => useOperationPolling("op-2"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_TIMEOUT_MS + 60_000);
    });
    expect(result.current.timedOut).toBe(true);
    expect(result.current.pending).toBe(false);
  });
});
