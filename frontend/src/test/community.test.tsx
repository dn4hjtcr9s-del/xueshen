// 社区重建前端测试（docs/community-rebuild-plan.md §十二 前端矩阵）：
// API client 契约 + 首页/详情/审核渲染 + 匿名行为。
import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  createBoardApplication,
  createPost,
  getBoardDetail,
  listAdminBoardApplications,
  listBoards,
  listMyBoardApplications,
  uploadAttachment,
  type CommunityBoard,
  type CommunityPostSummary,
} from "../api/community";
import { communityErrorMessage } from "../pages/community/format";
import CommunityHome from "../pages/community/CommunityHome";
import PostDetail from "../pages/community/PostDetail";
import AdminApplications from "../pages/community/AdminApplications";
import { server } from "./server";

const BOARD: CommunityBoard = {
  board_id: "da38ecb6-6f37-5724-be95-10e496b5f3dd",
  slug: "linear-algebra",
  name: "线性代数",
  description: "矩阵、向量空间、特征值与线性变换",
  post_count: 5,
  sort_order: 10,
};

const POST: CommunityPostSummary = {
  post_id: "11111111-1111-4111-8111-111111111111",
  board: BOARD,
  author: { display_name: "alice" },
  title: "大家都是怎么建立特征值的直觉的？",
  pinned: false,
  solved: false,
  reply_count: 2,
  like_count: 3,
  viewer_liked: false,
  created_at: "2026-08-14T01:00:00Z",
  last_activity_at: "2026-08-14T02:00:00Z",
  attachments: [],
};

const APPLICATION = {
  application_id: "22222222-2222-4222-8222-222222222222",
  name: "心理咨询",
  slug: "psych-counseling",
  description: "聊聊心事",
  reason: "希望有一个可以倾诉的地方",
  status: "pending",
  board_id: null,
  reviewed_at: null,
  reject_reason: null,
  created_at: "2026-08-20T01:00:00Z",
};

function publicError(status: number, code: string, message: string) {
  return HttpResponse.json(
    { error: { code, message, retryable: false, field: null, trace_id: "trace-test" } },
    { status },
  );
}

beforeEach(() => server.resetHandlers());

describe("community API client（重建新增接口）", () => {
  it("listBoards 返回 items（含 post_count）", async () => {
    server.use(
      http.get("*/memory-api/api/v1/community/boards", () =>
        HttpResponse.json({ items: [BOARD] }),
      ),
    );
    const boards = await listBoards();
    expect(boards).toHaveLength(1);
    expect(boards[0].post_count).toBe(5);
  });

  it("getBoardDetail 返回平铺板块对象（无包裹层）", async () => {
    server.use(
      http.get("*/memory-api/api/v1/community/boards/linear-algebra", () =>
        HttpResponse.json({
          ...BOARD,
          post_count: 5,
          created_at: "2026-08-01T00:00:00Z",
          viewer_is_owner: false,
        }),
      ),
    );
    const resp = await getBoardDetail("linear-algebra");
    expect(resp.slug).toBe("linear-algebra");
    expect(resp.viewer_is_owner).toBe(false);
  });

  it("createPost 携带 attachment_ids（顺序敏感）", async () => {
    let captured: Record<string, unknown> | null = null;
    server.use(
      http.post("*/memory-api/api/v1/community/posts", async ({ request }) => {
        captured = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({}, { status: 201 });
      }),
    );
    await createPost({
      board_id: BOARD.board_id,
      title: "t",
      body: "b",
      attachment_ids: ["a-1", "a-2"],
    });
    expect(captured).not.toBeNull();
    expect(captured!["attachment_ids"]).toEqual(["a-1", "a-2"]);
  });

  it("uploadAttachment 发送 multipart 表单 + Idempotency-Key", async () => {
    // 注：jsdom 环境 FormData 与 undici fetch 的序列化存在差异（Content-Type 显示
    // text/plain），multipart 断言只在真实浏览器有效；此处验证请求通道与幂等键。
    let idemKey: string | null = null;
    server.use(
      http.post("*/memory-api/api/v1/community/uploads", ({ request }) => {
        idemKey = request.headers.get("Idempotency-Key");
        return HttpResponse.json(
          {
            attachment_id: "33333333-3333-4333-8333-333333333333",
            url: "/api/v1/community/local-uploads/community/2026-08/x.png",
            mime: "image/png",
            width: 10,
            height: 10,
            size_bytes: 100,
          },
          { status: 201 },
        );
      }),
    );
    const file = new File(["x"], "x.png", { type: "image/png" });
    const uploaded = await uploadAttachment(file);
    expect(idemKey).toBeTruthy();
    expect(uploaded.attachment_id).toBe("33333333-3333-4333-8333-333333333333");
  });

  it("建吧申请接口：提交 / mine / admin 列表", async () => {
    server.use(
      http.post("*/memory-api/api/v1/community/applications", () =>
        HttpResponse.json(APPLICATION, { status: 201 }),
      ),
      http.get("*/memory-api/api/v1/community/applications/mine", () =>
        HttpResponse.json({ items: [APPLICATION], next_cursor: null, has_more: false }),
      ),
      http.get("*/memory-api/api/v1/community/admin/applications", ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("status")).toBe("all");
        return HttpResponse.json({ items: [APPLICATION], next_cursor: null, has_more: false });
      }),
    );
    const created = await createBoardApplication({
      name: "心理咨询",
      slug: "psych-counseling",
      description: "聊聊心事",
      reason: "希望有一个可以倾诉的地方",
    });
    expect(created.status).toBe("pending");
    const mine = await listMyBoardApplications();
    expect(mine.items).toHaveLength(1);
    const admin = await listAdminBoardApplications({ status: "all" });
    expect(admin.items[0].slug).toBe("psych-counseling");
  });

  it("409 BOARD_NAME_CONFLICT 映射为占用文案", async () => {
    server.use(
      http.post("*/memory-api/api/v1/community/applications", () =>
        publicError(409, "BOARD_NAME_CONFLICT", "该名称或标识已被占用"),
      ),
    );
    try {
      await createBoardApplication({ name: "n", slug: "nn", description: "", reason: "r" });
      expect.unreachable();
    } catch (e) {
      expect(communityErrorMessage(e, "申请提交失败")).toBe("该名称或标识已被占用");
    }
  });

  it("429 / 502 文案按 code 映射（不读 retryable）", async () => {
    const { MemoryApiError } = await import("../api/client");
    expect(
      communityErrorMessage(
        new MemoryApiError(
          429,
          { code: "COMMUNITY_RATE_LIMITED", message: "x", retryable: true },
          "fallback",
        ),
        "fallback",
      ),
    ).toBe("操作太频繁，请稍后再试");
    expect(
      communityErrorMessage(
        new MemoryApiError(
          502,
          {
            code: "COMMUNITY_UPLOAD_FAILED",
            message: "x",
            retryable: true,
          },
          "fallback",
        ),
        "fallback",
      ),
    ).toBe("服务繁忙，请稍后再试");
  });
});

describe("社区首页", () => {
  const noop = () => {};

  it("渲染板块宫格与帖子流", async () => {
    server.use(
      http.get("*/memory-api/api/v1/community/posts", () =>
        HttpResponse.json({ items: [POST], next_cursor: null, has_more: false }),
      ),
    );
    render(
      <CommunityHome
        boards={[BOARD]}
        boardsLoading={false}
        onOpenBoard={noop}
        onOpenPost={noop}
        onCreatePost={noop}
        onApply={noop}
        onAdmin={noop}
        isAdmin={false}
        isLoggedIn={false}
        onLoginRequired={noop}
      />,
    );
    expect(screen.getByText("线性代数")).toBeInTheDocument();
    expect(screen.getByText("5 帖")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByText("大家都是怎么建立特征值的直觉的？")).toBeInTheDocument(),
    );
  });

  it("帖子流加载失败显示重试", async () => {
    server.use(
      http.get("*/memory-api/api/v1/community/posts", () =>
        publicError(500, "INTERNAL_ERROR", "boom"),
      ),
    );
    render(
      <CommunityHome
        boards={[BOARD]}
        boardsLoading={false}
        onOpenBoard={noop}
        onOpenPost={noop}
        onCreatePost={noop}
        onApply={noop}
        onAdmin={noop}
        isAdmin={false}
        isLoggedIn={false}
        onLoginRequired={noop}
      />,
    );
    await waitFor(() => expect(screen.getByText("重试")).toBeInTheDocument());
  });

  it("管理员才渲染审核入口", () => {
    server.use(
      http.get("*/memory-api/api/v1/community/posts", () =>
        HttpResponse.json({ items: [], next_cursor: null, has_more: false }),
      ),
    );
    const { rerender } = render(
      <CommunityHome
        boards={[BOARD]}
        boardsLoading={false}
        onOpenBoard={noop}
        onOpenPost={noop}
        onCreatePost={noop}
        onApply={noop}
        onAdmin={noop}
        isAdmin={false}
        isLoggedIn={true}
        onLoginRequired={noop}
      />,
    );
    expect(screen.queryByText("建吧审核")).toBeNull();
    rerender(
      <CommunityHome
        boards={[BOARD]}
        boardsLoading={false}
        onOpenBoard={noop}
        onOpenPost={noop}
        onCreatePost={noop}
        onApply={noop}
        onAdmin={noop}
        isAdmin={true}
        isLoggedIn={true}
        onLoginRequired={noop}
      />,
    );
    expect(screen.getByText("建吧审核")).toBeInTheDocument();
  });
});

describe("帖子详情", () => {
  const DETAIL = {
    ...POST,
    title: "特征值直觉",
    body: "正文第一行\n第二行",
    deleted: false,
    discussion_status: "open",
    viewer_is_author: false,
    solved_reply_id: null,
    deleted_at: null,
    attachments: [
      { attachment_id: "a1", url: "/u/1.png", width: 100, height: 80, mime: "image/png", position: 0 },
      { attachment_id: "a2", url: "/u/2.png", width: 90, height: 70, mime: "image/png", position: 1 },
    ],
  };

  function mockDetail() {
    server.use(
      http.get(`*/memory-api/api/v1/community/posts/${POST.post_id}`, () =>
        HttpResponse.json({ post: DETAIL, replies: { items: [], next_cursor: null, has_more: false } }),
      ),
    );
  }

  it("正文保留换行渲染、配图按 position 序渲染", async () => {
    mockDetail();
    render(
      <PostDetail postId={POST.post_id} onBack={() => {}} isLoggedIn={false} onLoginRequired={() => {}} />,
    );
    await waitFor(() => expect(screen.getByText("特征值直觉")).toBeInTheDocument());
    const imgs = document.querySelectorAll(".comm-attachment img");
    expect(imgs).toHaveLength(2);
    expect(imgs[0].getAttribute("src")).toBe("/u/1.png");
    expect(imgs[1].getAttribute("src")).toBe("/u/2.png");
    expect(document.querySelector(".comm-detail-body")?.textContent).toContain("正文第一行");
  });

  it("匿名点回复发布触发 onLoginRequired，不调用 API", async () => {
    mockDetail();
    let replyCalls = 0;
    server.use(
      http.post(`*/memory-api/api/v1/community/posts/${POST.post_id}/replies`, () => {
        replyCalls += 1;
        return HttpResponse.json({}, { status: 201 });
      }),
    );
    const onLoginRequired = vi.fn();
    const { getByPlaceholderText, getByText } = render(
      <PostDetail postId={POST.post_id} onBack={() => {}} isLoggedIn={false} onLoginRequired={onLoginRequired} />,
    );
    await waitFor(() => expect(screen.getByText("特征值直觉")).toBeInTheDocument());
    const { fireEvent } = await import("@testing-library/react");
    fireEvent.change(getByPlaceholderText("写下你的回复…"), { target: { value: "你好" } });
    fireEvent.click(getByText("发布"));
    expect(onLoginRequired).toHaveBeenCalledTimes(1);
    expect(replyCalls).toBe(0);
  });

  it("用户内容纯文本渲染（<script> 原样显示）", async () => {
    server.use(
      http.get(`*/memory-api/api/v1/community/posts/${POST.post_id}`, () =>
        HttpResponse.json({
          post: { ...DETAIL, body: "<script>alert(1)</script>", attachments: [] },
          replies: { items: [], next_cursor: null, has_more: false },
        }),
      ),
    );
    render(
      <PostDetail postId={POST.post_id} onBack={() => {}} isLoggedIn={false} onLoginRequired={() => {}} />,
    );
    await waitFor(() =>
      expect(screen.getByText("<script>alert(1)</script>")).toBeInTheDocument(),
    );
    expect(document.querySelector(".comm-detail-body script")).toBeNull();
  });
});

describe("管理员审核视图", () => {
  it("已登录非管理员直达显示 403 提示卡", () => {
    render(<AdminApplications onBack={() => {}} isAdmin={false} />);
    expect(screen.getByText(/需要社区管理员权限/)).toBeInTheDocument();
  });

  it("管理员看到待审核列表与通过/拒绝操作", async () => {
    server.use(
      http.get("*/memory-api/api/v1/community/admin/applications", () =>
        HttpResponse.json({ items: [APPLICATION], next_cursor: null, has_more: false }),
      ),
    );
    render(<AdminApplications onBack={() => {}} isAdmin={true} />);
    await waitFor(() => expect(screen.getByText("心理咨询")).toBeInTheDocument());
    expect(screen.getByText("通过")).toBeInTheDocument();
    expect(screen.getByText("拒绝")).toBeInTheDocument();
  });
});
