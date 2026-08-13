// Conversation 前端测试（方案 §26.5）：API client + SSE reducer + 发送防双击。
import { renderHook, act, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createTurn, listConversations } from "../api/conversations";
import { startTurnEventStream } from "../api/turnEvents";
import { useConversation } from "../hooks/useConversation";
import { server } from "./server";
import type { SSEEnvelope } from "../types/conversation";

const THREAD = "11111111-1111-1111-1111-111111111111";
const TURN = "22222222-2222-2222-2222-222222222222";

function envelope(eventType: SSEEnvelope["event_type"], sequence: number, data: Record<string, unknown>): SSEEnvelope {
  return {
    schema_version: "1",
    event_id: String(sequence),
    sequence,
    event_type: eventType,
    request_id: "r",
    thread_id: THREAD,
    turn_id: TURN,
    run_id: "run",
    occurred_at: new Date().toISOString(),
    data,
  };
}

describe("conversations API client", () => {
  beforeEach(() => {
    server.resetHandlers();
  });

  it("列表请求带 cursor/limit 参数", async () => {
    server.use(
      http.get("*/memory-api/api/v1/conversations", ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("limit")).toBe("50");
        return HttpResponse.json({ items: [], next_cursor: null, has_more: false });
      }),
    );
    const page = await listConversations();
    expect(page.items).toEqual([]);
  });

  it("创建 Turn 返回 event_stream_path", async () => {
    server.use(
      http.post("*/memory-api/api/v1/conversations/:threadId/turns", () =>
        HttpResponse.json(
          {
            thread_id: THREAD,
            turn_id: TURN,
            user_message_id: "33333333-3333-3333-3333-333333333333",
            thread_version: 1,
            status: "accepted",
            event_stream_path: `/api/v1/conversations/${THREAD}/turns/${TURN}/events`,
          },
          { status: 202 },
        ),
      ),
    );
    const response = await createTurn(THREAD, {
      client_request_id: "req-1",
      content: "你好",
      expected_thread_version: 0,
    });
    expect(response.turn_id).toBe(TURN);
    expect(response.status).toBe("accepted");
  });

  it("THREAD_VERSION_CONFLICT 携带 current_version（附录 A.4）", async () => {
    server.use(
      http.post("*/memory-api/api/v1/conversations/:threadId/turns", () =>
        HttpResponse.json(
          {
            error: {
              code: "THREAD_VERSION_CONFLICT",
              message: "会话版本已变化",
              retryable: false,
              field: "expected_thread_version",
              trace_id: "t",
              current_version: 3,
            },
          },
          { status: 409 },
        ),
      ),
    );
    await expect(
      createTurn(THREAD, { client_request_id: "req-1", content: "x", expected_thread_version: 0 }),
    ).rejects.toMatchObject({ code: "THREAD_VERSION_CONFLICT" });
  });
});

describe("TurnEventStreamClient（§17.5 / Q15）", () => {
  beforeEach(() => {
    server.resetHandlers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("按 sequence 去重并传递事件", async () => {
    const events: SSEEnvelope[] = [];
    const lines = [
      envelope("answer.delta", 1, { text_delta: "勾" }),
      envelope("answer.delta", 1, { text_delta: "重复" }), // 重复序号
      envelope("answer.delta", 2, { text_delta: "股" }),
      envelope("answer.completed", 3, {
        assistant_message_id: "a",
        thread_version: 1,
        answer: "勾股",
        citations: [],
        followups: [],
        degraded_flags: [],
      }),
    ].map((e) => `id: ${e.sequence}\nevent: ${e.event_type}\ndata: ${JSON.stringify(e)}\n\n`);
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(lines.join("")));
        controller.close();
      },
    });
    // jsdom 下 MSW 对 ReadableStream body 支持有限，直接 stub fetch 返回 SSE 响应
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(body, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        }),
      ),
    );
    const stop = startTurnEventStream({
      url: `/api/v1/conversations/${THREAD}/turns/${TURN}/events`,
      onEvent: (e) => events.push(e),
    });
    await waitFor(() => expect(events.filter((e) => e.event_type === "answer.delta").length).toBe(2));
    stop();
    const deltas = events.filter((e) => e.event_type === "answer.delta");
    expect(deltas[0].data).toEqual({ text_delta: "勾" });
    expect(deltas[1].data).toEqual({ text_delta: "股" });
    expect(events.find((e) => e.event_type === "answer.completed")?.sequence).toBe(3);
  });
});

describe("useConversation（§18.2/§18.3）", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get("*/memory-api/api/v1/conversations", () =>
        HttpResponse.json({
          items: [{ thread_id: THREAD, title: "数学", status: "active", version: 0, updated_at: new Date().toISOString() }],
          next_cursor: null,
          has_more: false,
        }),
      ),
      http.get("*/memory-api/api/v1/conversations/:id", () =>
        HttpResponse.json({
          thread_id: THREAD,
          title: "数学",
          version: 0,
          status: "active",
          messages: [],
          next_cursor: null,
          has_more: false,
        }),
      ),
      http.post("*/memory-api/api/v1/conversations", () =>
        HttpResponse.json({ thread_id: THREAD, version: 0 }, { status: 201 }),
      ),
      http.post("*/memory-api/api/v1/conversations/:threadId/turns", () =>
        HttpResponse.json(
          {
            thread_id: THREAD,
            turn_id: TURN,
            user_message_id: "33333333-3333-3333-3333-333333333333",
            thread_version: 1,
            status: "accepted",
            event_stream_path: `/api/v1/conversations/${THREAD}/turns/${TURN}/events`,
          },
          { status: 202 },
        ),
      ),
    );
  });

  it("加载会话列表并打开会话", async () => {
    const { result } = renderHook(() => useConversation());
    await waitFor(() => expect(result.current.threads.length).toBe(1));
    expect(result.current.threads[0].title).toBe("数学");
    await act(async () => {
      await result.current.openThread(THREAD);
    });
    expect(result.current.detail?.thread_id).toBe(THREAD);
  });
});
