// Community 写交互前端测试（方案 §15.3，PR-C）：发帖/回复/点赞/解决/删除。
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CommunityPostSummary } from "../api/community";
import { CommunityPage } from "../pages/Community";
import { server } from "./server";

const POST: CommunityPostSummary = {
  post_id: "11111111-1111-4111-8111-111111111111",
  board: {
    board_id: "da38ecb6-6f37-5724-be95-10e496b5f3dd",
    slug: "linear-algebra",
    name: "线性代数",
    description: "",
  },
  author: { display_name: "alice" },
  title: "特征值直觉",
  pinned: false,
  solved: false,
  reply_count: 0,
  like_count: 0,
  viewer_liked: false,
  created_at: "2026-08-14T01:00:00Z",
  last_activity_at: "2026-08-14T02:00:00Z",
};

const DETAIL = {
  ...POST,
  title: "特征值直觉",
  body: "正文内容",
  deleted: false,
  discussion_status: "open",
  viewer_is_author: true,
  solved_reply_id: null,
  deleted_at: null,
};

function mockDetailFlow(overrides: Record<string, unknown> = {}) {
  server.use(
    http.get("*/memory-api/api/v1/community/boards", () =>
      HttpResponse.json({ items: [POST.board] }),
    ),
    http.get("*/memory-api/api/v1/community/posts", () =>
      HttpResponse.json({ items: [POST], next_cursor: null, has_more: false }),
    ),
    http.get("*/memory-api/api/v1/community/posts/:postId", () =>
      HttpResponse.json({
        post: { ...DETAIL, ...overrides },
        replies: { items: [], next_cursor: null, has_more: false },
      }),
    ),
  );
}

function openDetail() {
  render(<CommunityPage />);
  return screen.findAllByText(POST.title).then(() => {
    const row = screen.getByText(POST.title).closest(".post-row");
    row!.dispatchEvent(new MouseEvent("click", { bubbles: true }));
    return waitFor(() => expect(screen.getByText("正文内容")).toBeInTheDocument());
  });
}

describe("Community 写交互", () => {
  beforeEach(() => server.resetHandlers());

  it("发帖面板提交成功后刷新列表", async () => {
    const create = vi.fn();
    server.use(
      http.get("*/memory-api/api/v1/community/boards", () =>
        HttpResponse.json({ items: [POST.board] }),
      ),
      http.get("*/memory-api/api/v1/community/posts", () =>
        HttpResponse.json({ items: [], next_cursor: null, has_more: false }),
      ),
      http.post("*/memory-api/api/v1/community/posts", async ({ request }) => {
        create(await request.json());
        return HttpResponse.json(DETAIL, { status: 201 });
      }),
    );
    render(<CommunityPage />);
    await waitFor(() => {
      expect(screen.getByText("发起讨论")).toBeInTheDocument();
    });
    screen.getByText("发起讨论").click();
    const inputs = await screen.findAllByRole("textbox");
    fireEvent.change(inputs[0], { target: { value: "新帖标题" } });
    fireEvent.change(inputs[1], { target: { value: "新帖正文" } });
    const publish = screen.getByText("发布");
    await waitFor(() => expect((publish as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(publish);
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toEqual({
      board_id: POST.board.board_id,
      title: "新帖标题",
      body: "新帖正文",
    });
  });

  it("回复提交调用创建接口并清空输入框", async () => {
    const create = vi.fn();
    mockDetailFlow();
    server.use(
      http.post("*/memory-api/api/v1/community/posts/:postId/replies", async ({ request }) => {
        create(await request.json());
        return HttpResponse.json(
          {
            reply_id: "22222222-2222-4222-8222-222222222222",
            author: { display_name: "alice" },
            body: "我的回复",
            deleted: false,
            viewer_is_author: true,
            solved: false,
            created_at: "2026-08-14T03:00:00Z",
          },
          { status: 201 },
        );
      }),
    );
    await openDetail();
    const textarea = screen.getAllByRole("textbox")[0] as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "我的回复" } });
    fireEvent.click(screen.getByText("发布"));
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toEqual({ body: "我的回复" });
  });

  it("点赞/取消点赞切换", async () => {
    const like = vi.fn();
    const unlike = vi.fn();
    mockDetailFlow();
    server.use(
      http.post("*/memory-api/api/v1/community/posts/:postId/like", () => {
        like();
        return HttpResponse.json({ status: "ok" });
      }),
      http.delete("*/memory-api/api/v1/community/posts/:postId/like", () => {
        unlike();
        return HttpResponse.json({ status: "ok" });
      }),
    );
    await openDetail();
    fireEvent.click(screen.getByText("0"));
    await waitFor(() => expect(like).toHaveBeenCalledTimes(1));
  });

  it("作者删除帖子需确认（§6.6 弹层文案）；确认后调用删除接口并返回列表", async () => {
    const remove = vi.fn();
    mockDetailFlow();
    server.use(
      http.delete("*/memory-api/api/v1/community/posts/:postId", () => {
        remove();
        return HttpResponse.json({ status: "ok" });
      }),
    );
    await openDetail();
    const deleteBtn = screen.getAllByText("删除")[0];
    fireEvent.click(deleteBtn);
    // 冻结文案（§6.6）：确认弹层出现；未确认不触发删除
    await waitFor(() => {
      expect(screen.getByText("删除这条帖子？")).toBeInTheDocument();
    });
    expect(remove).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("确认删除"));
    await waitFor(() => expect(remove).toHaveBeenCalledTimes(1));
  });

  it("解决状态切换调用 resolve 接口（标记/取消）", async () => {
    const resolve = vi.fn();
    mockDetailFlow();
    server.use(
      http.get("*/memory-api/api/v1/community/posts/:postId", () =>
        HttpResponse.json({
          post: {
            ...DETAIL,
            solved: true,
            solved_reply_id: "22222222-2222-4222-8222-222222222222",
          },
          replies: {
            items: [
              {
                reply_id: "22222222-2222-4222-8222-222222222222",
                author: { display_name: "bob" },
                body: "答案",
                deleted: false,
                viewer_is_author: false,
                solved: true,
                created_at: "2026-08-14T03:00:00Z",
              },
            ],
            next_cursor: null,
            has_more: false,
          },
        }),
      ),
      http.post("*/memory-api/api/v1/community/posts/:postId/resolve", async ({ request }) => {
        resolve(await request.json());
        return HttpResponse.json({ status: "ok" });
      }),
    );
    await openDetail();
    await waitFor(() => expect(screen.getByText("取消解决")).toBeInTheDocument());
    fireEvent.click(screen.getByText("取消解决"));
    await waitFor(() => expect(resolve).toHaveBeenCalledTimes(1));
    expect(resolve.mock.calls[0][0]).toEqual({ reply_id: null });
  });

  it("通知跳转：targetPostId 直接打开详情并消费", async () => {
    mockDetailFlow();
    const consumed = vi.fn();
    const { rerender } = render(
      <CommunityPage targetPostId={null} onTargetConsumed={consumed} />,
    );
    rerender(
      <CommunityPage targetPostId={POST.post_id} onTargetConsumed={consumed} />,
    );
    await waitFor(() => expect(screen.getByText("正文内容")).toBeInTheDocument());
    await waitFor(() => expect(consumed).toHaveBeenCalled());
  });

  it("通知跳转时先切到讨论区 Tab（§6.5#5）", async () => {
    mockDetailFlow();
    const { rerender } = render(<CommunityPage />);
    // 切到其他 Tab
    fireEvent.click(screen.getByText("打卡圈"));
    expect(screen.getByText("即将开放")).toBeInTheDocument();
    rerender(<CommunityPage targetPostId={POST.post_id} onTargetConsumed={() => {}} />);
    // target 到达后自动切回讨论区并打开详情（不被"打卡圈"Tab 清掉）
    await waitFor(() => expect(screen.getByText("正文内容")).toBeInTheDocument());
    expect(screen.queryByText("该帖子已被作者删除")).not.toBeInTheDocument();
  });
});
