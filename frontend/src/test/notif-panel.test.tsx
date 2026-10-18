// App 通知面板测试（方案 §6.5，PR-C）：双域合并、局部失败、红点、read-all 部分失败。
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { AuthProvider } from "../auth/AuthContext";
import { server } from "./server";

const MEMORY_NOTIF = {
  notification_id: "m1",
  event_type: "activity_evidence",
  title: "记忆已更新",
  body: "特征值",
  aggregate_type: "activity",
  aggregate_id: "a1",
  read_at: null,
  created_at: "2026-08-14T01:00:00Z",
};

const COMMUNITY_NOTIF = {
  notification_id: "c1",
  event_type: "post_replied",
  title: "bob 回复了你的帖子",
  body: "回复内容",
  read_at: null,
  created_at: "2026-08-14T02:00:00Z",
  post_id: "11111111-1111-4111-8111-111111111111",
  reply_id: "22222222-2222-4222-8222-222222222222",
};

const LOGIN_USER = {
  user_id: "00000000-0000-0000-0000-000000000a01",
  username: "alice01",
  email: null,
  status: "active",
  created_at: "2026-08-13T00:00:00+00:00",
};

function renderApp() {
  return render(
    <AuthProvider>
      <App />
    </AuthProvider>,
  );
}

describe("App 通知面板（§6.5）", () => {
  beforeEach(() => {
    server.resetHandlers();
    // 审计 P1-2 后通知只在"鉴权就绪且已登录"时加载：refresh 返回已登录用户，
    // 登录后 App 自动拉取通知（无需先点铃铛）；首页活动判定的三个接口给空数据。
    server.use(
      http.post("*/api/v1/auth/refresh", () =>
        HttpResponse.json({ access_token: "test-access-token", token_type: "bearer", expires_in: 300, user: LOGIN_USER }),
      ),
      http.get("*/memory-api/api/v1/conversations", () =>
        HttpResponse.json({ items: [], next_cursor: null, has_more: false }),
      ),
      http.get("*/memory-api/api/v1/memory/index", () =>
        HttpResponse.json({ version: 0, entries: [], updated_at: null, stale: false }),
      ),
      http.get("*/memory-api/api/v1/knowledge-graph/me/nodes", () => HttpResponse.json([])),
    );
  });

  it("双域通知合并按 created_at DESC 排序，key 含 source", async () => {
    server.use(
      http.get("*/memory-api/api/v1/memory/notifications", () =>
        HttpResponse.json({
          items: [MEMORY_NOTIF],
          next_cursor: null,
          has_more: false,
          unread_count: 1,
        }),
      ),
      http.get("*/memory-api/api/v1/community/notifications", () =>
        HttpResponse.json({
          items: [COMMUNITY_NOTIF],
          next_cursor: null,
          has_more: false,
          unread_count: 1,
        }),
      ),
    );
    renderApp();
    const bell = screen.getByLabelText("通知");
    bell.click();
    await waitFor(() => {
      expect(screen.getByText(/bob 回复了你的帖子/)).toBeInTheDocument();
    });
    const items = document.querySelectorAll(".notif-item");
    expect(items).toHaveLength(2);
    // 稳定排序：created_at DESC（社区 02:00 > Memory 01:00）
    expect(items[0].textContent).toContain("bob 回复了你的帖子");
    expect(items[0].textContent).toContain("社区");
    // 未读红点：两个域 unread_count 之和
    expect(document.querySelector(".dot")).not.toBeNull();
  });

  it("局部失败：Memory 域失败仍展示社区通知并提示", async () => {
    server.use(
      http.get("*/memory-api/api/v1/memory/notifications", () =>
        HttpResponse.json({ error: { code: "X", message: "boom" } }, { status: 500 }),
      ),
      http.get("*/memory-api/api/v1/community/notifications", () =>
        HttpResponse.json({
          items: [COMMUNITY_NOTIF],
          next_cursor: null,
          has_more: false,
          unread_count: 1,
        }),
      ),
    );
    renderApp();
    screen.getByLabelText("通知").click();
    await waitFor(() => {
      expect(screen.getByText(/bob 回复了你的帖子/)).toBeInTheDocument();
    });
    expect(screen.getByText(/Memory 通知加载失败/)).toBeInTheDocument();
  });

  it("全部已读并发调用两域 read-all；部分失败提示", async () => {
    const memoryReadAll = vi.fn();
    const communityReadAll = vi.fn();
    server.use(
      http.get("*/memory-api/api/v1/memory/notifications", () =>
        HttpResponse.json({
          items: [MEMORY_NOTIF],
          next_cursor: null,
          has_more: false,
          unread_count: 1,
        }),
      ),
      http.get("*/memory-api/api/v1/community/notifications", () =>
        HttpResponse.json({
          items: [COMMUNITY_NOTIF],
          next_cursor: null,
          has_more: false,
          unread_count: 1,
        }),
      ),
      http.post("*/memory-api/api/v1/memory/notifications/read-all", () => {
        memoryReadAll();
        return HttpResponse.json({ unread_count: 0 });
      }),
      http.post("*/memory-api/api/v1/community/notifications/read-all", () => {
        communityReadAll();
        return HttpResponse.json({ error: { code: "X", message: "boom" } }, { status: 500 });
      }),
    );
    renderApp();
    screen.getByLabelText("通知").click();
    await waitFor(() => {
      expect(screen.getByText("全部已读")).toBeInTheDocument();
    });
    screen.getByText("全部已读").click();
    await waitFor(() => expect(memoryReadAll).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(communityReadAll).toHaveBeenCalledTimes(1));
    // 部分失败提示（两个域均被刷新）
    await waitFor(() => {
      expect(screen.getByText("部分通知标记失败")).toBeInTheDocument();
    });
  });

  it("点击社区通知切换到社区页并打开详情（target 清空）", async () => {
    server.use(
      http.get("*/memory-api/api/v1/memory/notifications", () =>
        HttpResponse.json({
          items: [],
          next_cursor: null,
          has_more: false,
          unread_count: 0,
        }),
      ),
      http.get("*/memory-api/api/v1/community/notifications", () =>
        HttpResponse.json({
          items: [COMMUNITY_NOTIF],
          next_cursor: null,
          has_more: false,
          unread_count: 1,
        }),
      ),
      http.get("*/memory-api/api/v1/community/boards", () =>
        HttpResponse.json({ items: [] }),
      ),
      http.get("*/memory-api/api/v1/community/posts", () =>
        HttpResponse.json({ items: [], next_cursor: null, has_more: false }),
      ),
      http.get("*/memory-api/api/v1/community/posts/:postId", () =>
        HttpResponse.json({
          post: {
            post_id: COMMUNITY_NOTIF.post_id,
            board: { board_id: "b1", slug: "s", name: "线性代数", description: "" },
            author: { display_name: "alice" },
            title: "帖子标题",
            body: "帖子正文",
            pinned: false,
            solved: false,
            reply_count: 0,
            like_count: 0,
            viewer_liked: false,
            created_at: "2026-08-14T01:00:00Z",
            last_activity_at: "2026-08-14T01:00:00Z",
            deleted: false,
            discussion_status: "open",
            viewer_is_author: false,
            solved_reply_id: null,
            deleted_at: null,
          },
          replies: { items: [], next_cursor: null, has_more: false },
        }),
      ),
    );
    renderApp();
    screen.getByLabelText("通知").click();
    await waitFor(() => {
      expect(screen.getByText(/bob 回复了你的帖子/)).toBeInTheDocument();
    });
    document.querySelector(".notif-item")!.dispatchEvent(
      new MouseEvent("click", { bubbles: true }),
    );
    await waitFor(() => {
      expect(screen.getByText("帖子正文")).toBeInTheDocument();
    });
  });
});
