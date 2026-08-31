// Conversation 前端测试（方案 §26.5）：API client + SSE reducer + 发送防双击。
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createConversation, createTurn, listConversations } from "../api/conversations";
import { startTurnEventStream } from "../api/turnEvents";
import { resolveMemoryApiUrl } from "../api/client";
import { useConversation } from "../hooks/useConversation";
import { useTurnStream } from "../hooks/useTurnStream";
import { ChatPage } from "../pages/Chat";
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

  it("创建会话使用共享请求层的相对路径", async () => {
    server.use(
      http.post("/memory-api/api/v1/conversations", () =>
        HttpResponse.json({ thread_id: THREAD, version: 0 }, { status: 201 }),
      ),
    );
    await expect(createConversation()).resolves.toEqual({ thread_id: THREAD, version: 0 });
  });

  it("创建 Turn 返回 event_stream_path", async () => {
    server.use(
      http.post("*/memory-api/api/v1/conversations/:threadId/turns", async ({ request }) => {
        expect(await request.json()).toEqual({
          client_request_id: "req-1",
          content: "你好",
          expected_thread_version: 0,
        });
        return HttpResponse.json(
          {
            thread_id: THREAD,
            turn_id: TURN,
            user_message_id: "33333333-3333-3333-3333-333333333333",
            thread_version: 1,
            status: "accepted",
            event_stream_path: `/api/v1/conversations/${THREAD}/turns/${TURN}/events`,
          },
          { status: 202 },
        );
      }),
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

describe("SSE 地址解析", () => {
  it("把后端返回的 /api/v1 路径接到当前 Memory API 前缀", () => {
    expect(resolveMemoryApiUrl(`/api/v1/conversations/${THREAD}/turns/${TURN}/events`)).toBe(
      `/memory-api/api/v1/conversations/${THREAD}/turns/${TURN}/events`,
    );
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


  it("断开终态流后保留失败提示，直到调用方显式重置", async () => {
    const failed = envelope("turn.failed", 1, {
      error: {
        code: "CONVERSATION_RUN_FAILED",
        message: "回答失败，请重试",
        retryable: true,
        trace_id: "trace-1",
      },
    });
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(
          new TextEncoder().encode(
            `id: ${failed.sequence}\nevent: ${failed.event_type}\ndata: ${JSON.stringify(failed)}\n\n`,
          ),
        );
        controller.close();
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(body, {
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
        }),
      ),
    );

    const initialProps: { url: string | null } = {
      url: `/api/v1/conversations/${THREAD}/turns/${TURN}/events`,
    };
    const { result, rerender } = renderHook(
      ({ url }: { url: string | null }) => useTurnStream(url),
      { initialProps },
    );
    await waitFor(() => expect(result.current.state.status).toBe("failed"));

    rerender({ url: null });
    expect(result.current.state.status).toBe("failed");
    expect(result.current.state.error?.message).toBe("回答失败，请重试");

    act(() => result.current.reset());
    expect(result.current.state.status).toBe("idle");
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

  it("首次发送时自动创建后端会话", async () => {
    let createCount = 0;
    let turnBody: unknown = null;
    server.use(
      http.get("*/memory-api/api/v1/conversations", () =>
        HttpResponse.json({ items: [], next_cursor: null, has_more: false }),
      ),
      http.post("*/memory-api/api/v1/conversations", () => {
        createCount += 1;
        return HttpResponse.json({ thread_id: THREAD, version: 0 }, { status: 201 });
      }),
      http.post("*/memory-api/api/v1/conversations/:threadId/turns", async ({ request }) => {
        turnBody = await request.json();
        return HttpResponse.json(
          {
            thread_id: THREAD,
            turn_id: TURN,
            user_message_id: "33333333-3333-3333-3333-333333333333",
            thread_version: 1,
            status: "accepted",
            event_stream_path: `/api/v1/conversations/${THREAD}/turns/${TURN}/events`,
          },
          { status: 202 },
        );
      }),
    );

    const { result } = renderHook(() => useConversation());
    await waitFor(() => expect(result.current.threads).toEqual([]));
    await act(async () => {
      await result.current.send("帮我复习极限");
    });

    expect(createCount).toBe(1);
    expect(turnBody).toMatchObject({ content: "帮我复习极限", expected_thread_version: 0 });
    expect(result.current.activeThreadId).toBe(THREAD);
    expect(result.current.detail?.messages[0]?.content).toBe("帮我复习极限");
  });

  it("新对话只清空前端选择，不立即创建空会话", async () => {
    let createCount = 0;
    server.use(
      http.post("*/memory-api/api/v1/conversations", () => {
        createCount += 1;
        return HttpResponse.json({ thread_id: THREAD, version: 0 }, { status: 201 });
      }),
    );
    const { result } = renderHook(() => useConversation());
    await waitFor(() => expect(result.current.threads.length).toBe(1));
    await act(async () => {
      await result.current.openThread(THREAD);
    });

    act(() => result.current.newThread());

    expect(result.current.activeThreadId).toBeNull();
    expect(result.current.detail).toBeNull();
    expect(createCount).toBe(0);
  });
});

describe("ChatPage 欢迎态", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get("*/memory-api/api/v1/conversations", () =>
        HttpResponse.json({ items: [], next_cursor: null, has_more: false }),
      ),
    );
  });

  it("没有历史会话时展示欢迎语、四个入口和左上角新对话", async () => {
    render(<ChatPage />);

    expect(await screen.findByRole("heading", { name: "要在 xueshen 里学习什么？" })).toBeVisible();
    expect(screen.getByRole("button", { name: "新对话" })).toBeVisible();
    expect(screen.queryByRole("complementary", { name: "历史对话" })).not.toBeInTheDocument();
    expect(screen.getAllByRole("button").filter((button) => button.classList.contains("starter-card"))).toHaveLength(4);
  });

  it("点击功能入口把对应提示词填入输入框", async () => {
    const user = userEvent.setup();
    render(<ChatPage />);

    await user.click(await screen.findByRole("button", { name: "为自己设置学习计划" }));

    expect(screen.getByRole<HTMLInputElement>("textbox", { name: "对话输入" }).value).toContain(
      "制定一个合理、循序渐进的学习计划",
    );
    expect(screen.getByRole("button", { name: "发送" })).toBeEnabled();
  });

  it("有真实历史会话时在左侧栏显示对话", async () => {
    server.use(
      http.get("*/memory-api/api/v1/conversations", () =>
        HttpResponse.json({
          items: [
            {
              thread_id: THREAD,
              title: "极限复习",
              status: "active",
              version: 1,
              updated_at: "2026-08-30T08:00:00Z",
            },
          ],
          next_cursor: null,
          has_more: false,
        }),
      ),
    );

    render(<ChatPage />);

    expect(await screen.findByRole("complementary", { name: "历史对话" })).toBeVisible();
    expect(screen.getByRole("button", { name: /极限复习/ })).toBeVisible();
  });
});

describe("Turn stream reducer", () => {
  it("连续事件在同一批次到达时仍保留流程与回答内容", async () => {
    const { initialTurnStreamState, reduceTurnStreamState } = await import("../hooks/useTurnStream");
    const progressStarted = envelope("turn.progress", 1, {
      stage: "memory",
      status: "started",
      title: "正在读取长期记忆",
      metadata: {},
    });
    const progressCompleted = envelope("turn.progress", 2, {
      stage: "memory",
      status: "completed",
      title: "已读取相关记忆",
      detail: "学习记录已加入上下文",
      metadata: { memory_status: "available" },
    });
    const delta = envelope("answer.delta", 3, { text_delta: "答案" });
    const next = reduceTurnStreamState(
      reduceTurnStreamState(
        reduceTurnStreamState(initialTurnStreamState, progressStarted),
        progressCompleted,
      ),
      delta,
    );

    expect(next.answer).toBe("答案");
    expect(next.progress).toHaveLength(1);
    expect(next.progress[0]).toMatchObject({
      stage: "memory",
      status: "completed",
      title: "已读取相关记忆",
    });
  });
});
