// Community 前端测试（方案 §15.3，PR-B）：API client + 讨论区列表/详情渲染。
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import {
  getPostDetail,
  listBoards,
  listPosts,
  type CommunityPostSummary,
} from "../api/community";
import { CommunityPage } from "../pages/Community";
import { server } from "./server";

const POST: CommunityPostSummary = {
  post_id: "11111111-1111-4111-8111-111111111111",
  board: {
    board_id: "da38ecb6-6f37-5724-be95-10e496b5f3dd",
    slug: "linear-algebra",
    name: "线性代数",
    description: "矩阵、向量空间、特征值与线性变换",
  },
  author: { display_name: "alice" },
  title: "大家都是怎么建立特征值的直觉的？",
  pinned: false,
  solved: false,
  reply_count: 2,
  like_count: 3,
  viewer_liked: false,
  created_at: "2026-08-14T01:00:00Z",
  last_activity_at: "2026-08-14T02:00:00Z",
};

const BOARDS = [
  { board_id: "da38ecb6-6f37-5724-be95-10e496b5f3dd", slug: "linear-algebra", name: "线性代数", description: "" },
  { board_id: "dcd2a3a5-7e06-5b7e-891f-e065765dcde0", slug: "calculus", name: "微积分", description: "" },
];

describe("community API client", () => {
  beforeEach(() => server.resetHandlers());

  it("401 refresh 后幂等重试不重复发帖（幂等键保持不变）", async () => {
    // §15.3：401 → single-flight refresh → 重放；createPost 只重放一次且
    // Idempotency-Key 不变（客户端幂等键保证服务端去重）
    const { setAccessToken } = await import("../auth/tokenStore");
    setAccessToken("stale-token");
    let postCalls = 0;
    let refreshCalls = 0;
    const capturedKeys: string[] = [];
    server.use(
      http.post("*/api/v1/auth/refresh", () => {
        refreshCalls += 1;
        return HttpResponse.json({ access_token: "refreshed-token" });
      }),
      http.post("*/memory-api/api/v1/community/posts", ({ request }) => {
        postCalls += 1;
        capturedKeys.push(request.headers.get("Idempotency-Key") ?? "");
        if (request.headers.get("Authorization") !== "Bearer refreshed-token") {
          return new HttpResponse(null, { status: 401 });
        }
        return HttpResponse.json(
          {
            ...POST,
            body: "正文",
            deleted: false,
            discussion_status: "open",
            viewer_is_author: true,
            solved_reply_id: null,
            deleted_at: null,
          },
          { status: 201 },
        );
      }),
    );
    const { createPost } = await import("../api/community");
    const resp = await createPost({ board_id: "b1", title: "t", body: "b" });
    expect(resp.post_id).toBe(POST.post_id);
    expect(refreshCalls).toBe(1);
    expect(postCalls).toBe(2); // 1 次 401 + 1 次重放
    // 幂等键在 401 重试中保持不变 → 服务端可按同键去重（§8.3）
    expect(capturedKeys[0]).toBe(capturedKeys[1]);
    expect(capturedKeys[0]).toMatch(/^[0-9a-f-]{36}$/);
  });

  it("帖子列表请求带筛选/游标参数", async () => {
    server.use(
      http.get("*/memory-api/api/v1/community/posts", ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("sort")).toBe("unanswered");
        expect(url.searchParams.get("board_id")).toBe(BOARDS[0].board_id);
        expect(url.searchParams.get("cursor")).toBe("c1");
        return HttpResponse.json({
          items: [POST],
          next_cursor: null,
          has_more: false,
        });
      }),
    );
    const page = await listPosts({
      board_id: BOARDS[0].board_id,
      sort: "unanswered",
      cursor: "c1",
    });
    expect(page.items[0].title).toBe(POST.title);
    expect(page.has_more).toBe(false);
  });

  it("板块列表返回 items 数组", async () => {
    server.use(
      http.get("*/memory-api/api/v1/community/boards", () =>
        HttpResponse.json({ items: BOARDS }),
      ),
    );
    expect(await listBoards()).toHaveLength(2);
  });

  it("详情接口返回帖子和回复分页", async () => {
    server.use(
      http.get("*/memory-api/api/v1/community/posts/:postId", () =>
        HttpResponse.json({
          post: { ...POST, body: "正文", deleted: false, discussion_status: "open",
                  viewer_is_author: true, solved_reply_id: null, deleted_at: null },
          replies: { items: [], next_cursor: null, has_more: false },
        }),
      ),
    );
    const resp = await getPostDetail({ post_id: POST.post_id });
    expect(resp.post.body).toBe("正文");
  });
});

describe("CommunityPage 讨论区", () => {
  beforeEach(() => {
    server.resetHandlers();
    server.use(
      http.get("*/memory-api/api/v1/community/boards", () =>
        HttpResponse.json({ items: BOARDS }),
      ),
    );
  });

  it("列表渲染帖子行并支持点击进入详情", async () => {
    server.use(
      http.get("*/memory-api/api/v1/community/posts", () =>
        HttpResponse.json({ items: [POST], next_cursor: null, has_more: false }),
      ),
      http.get("*/memory-api/api/v1/community/posts/:postId", () =>
        HttpResponse.json({
          post: { ...POST, body: "正文内容", deleted: false, discussion_status: "open",
                  viewer_is_author: true, solved_reply_id: null, deleted_at: null },
          replies: {
            items: [
              { reply_id: "22222222-2222-4222-8222-222222222222",
                author: { display_name: "bob" }, body: "可以先从线性变换理解。",
                deleted: false, viewer_is_author: false, solved: false,
                created_at: "2026-08-14T03:00:00Z" },
            ],
            next_cursor: null,
            has_more: false,
          },
        }),
      ),
    );
    render(<CommunityPage />);
    await waitFor(() => {
      expect(screen.getByText(POST.title)).toBeInTheDocument();
    });
    // 点击帖子行（标题唯一；板块名同时出现在筛选 chip 与帖子 tag 中）
    const row = screen.getByText(POST.title).closest(".post-row");
    expect(row).not.toBeNull();
    row!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await waitFor(() => {
      expect(screen.getByText("正文内容")).toBeInTheDocument();
    });
    expect(screen.getByText("可以先从线性变换理解。")).toBeInTheDocument();
  });

  it("空态与错误重试", async () => {
    let fail = true;
    server.use(
      http.get("*/memory-api/api/v1/community/posts", () => {
        if (fail) return HttpResponse.json({ error: { code: "X", message: "boom" } }, { status: 500 });
        return HttpResponse.json({ items: [POST], next_cursor: null, has_more: false });
      }),
    );
    render(<CommunityPage />);
    await waitFor(() => {
      expect(screen.getByText("重试")).toBeInTheDocument();
    });
    fail = false;
    screen.getByText("重试").click();
    await waitFor(() => {
      expect(screen.getByText(POST.title)).toBeInTheDocument();
    });
  });

  it("空列表显示冻结空态文案", async () => {
    server.use(
      http.get("*/memory-api/api/v1/community/posts", () =>
        HttpResponse.json({ items: [], next_cursor: null, has_more: false }),
      ),
    );
    render(<CommunityPage />);
    await waitFor(() => {
      expect(screen.getByText("还没有帖子，来发起第一个讨论吧")).toBeInTheDocument();
    });
  });

  it("墓碑详情不泄露原正文（即使后端违约返回 body 也不渲染）", async () => {
    server.use(
      http.get("*/memory-api/api/v1/community/posts", () =>
        HttpResponse.json({ items: [POST], next_cursor: null, has_more: false }),
      ),
      http.get("*/memory-api/api/v1/community/posts/:postId", () =>
        // 模拟后端契约违约：deleted=true 但仍带正文（真实泄露场景）
        HttpResponse.json({
          post: {
            ...POST,
            title: "SECRET_LEAK_TITLE",
            body: "SECRET_LEAK_BODY",
            deleted: true,
            discussion_status: "closed",
            viewer_is_author: true,
            solved_reply_id: null,
            deleted_at: "2026-08-14T04:00:00Z",
          },
          replies: { items: [], next_cursor: null, has_more: false },
        }),
      ),
    );
    render(<CommunityPage />);
    await waitFor(() => {
      expect(screen.getByText(POST.title)).toBeInTheDocument();
    });
    const row = screen.getByText(POST.title).closest(".post-row");
    row!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    await waitFor(() => {
      expect(screen.getByText("该帖子已被作者删除")).toBeInTheDocument();
    });
    // 组件层面保证：墓碑渲染不泄露原正文（§6.6/§15.3）
    expect(screen.queryByText("SECRET_LEAK_TITLE")).not.toBeInTheDocument();
    expect(screen.queryByText("SECRET_LEAK_BODY")).not.toBeInTheDocument();
  });
});
