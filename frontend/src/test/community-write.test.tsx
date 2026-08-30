// 社区写交互测试（§十二 前端矩阵）：发帖图片交互、点赞、删除确认。
// 注：jsdom 环境 FormData 与 undici fetch 序列化不兼容，CreatePost 的上传/发帖
// 走 vi.mock 的 api 层（真实请求通道由 community.test.tsx 的 client 测试覆盖）。
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryApiError } from "../api/client";
import type { CommunityBoard, CommunityPostSummary } from "../api/community";
import CreatePost from "../pages/community/CreatePost";
import PostDetail from "../pages/community/PostDetail";
import { server } from "./server";

vi.mock("../api/community", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/community")>();
  return {
    ...actual,
    uploadAttachment: vi.fn(),
    createPost: vi.fn(),
  };
});

import { createPost, uploadAttachment } from "../api/community";

const uploadMock = vi.mocked(uploadAttachment);
const createPostMock = vi.mocked(createPost);

// jsdom 无 objectURL：补齐 stub（CreatePost 本地预览依赖）
if (typeof URL.createObjectURL !== "function") {
  URL.createObjectURL = vi.fn(() => "blob:mock-preview");
  URL.revokeObjectURL = vi.fn();
}

const BOARD: CommunityBoard = {
  board_id: "da38ecb6-6f37-5724-be95-10e496b5f3dd",
  slug: "linear-algebra",
  name: "线性代数",
  description: "",
  post_count: 0,
  sort_order: 10,
};

const POST: CommunityPostSummary = {
  post_id: "11111111-1111-4111-8111-111111111111",
  board: BOARD,
  author: { display_name: "alice" },
  title: "特征值直觉",
  pinned: false,
  solved: false,
  reply_count: 0,
  like_count: 0,
  viewer_liked: false,
  created_at: "2026-08-14T01:00:00Z",
  last_activity_at: "2026-08-14T02:00:00Z",
  attachments: [],
};

function makeFile(name: string): File {
  return new File(["img-bytes"], name, { type: "image/png" });
}

function pickImages(input: HTMLInputElement, count: number) {
  const files = Array.from({ length: count }, (_, i) => makeFile(`p${i}.png`));
  fireEvent.change(input, { target: { files } });
}

function renderCreatePost(overrides: Partial<Parameters<typeof CreatePost>[0]> = {}) {
  return render(
    <CreatePost
      boardId={BOARD.board_id}
      boards={[BOARD]}
      onDone={vi.fn()}
      onCancel={vi.fn()}
      isLoggedIn={true}
      onLoginRequired={vi.fn()}
      {...overrides}
    />,
  );
}

function fillForm() {
  fireEvent.change(screen.getByPlaceholderText(/标题/), { target: { value: "测试标题" } });
  fireEvent.change(screen.getByPlaceholderText(/正文/), { target: { value: "测试正文" } });
}

beforeEach(() => server.resetHandlers());

describe("发帖图片交互", () => {
  it("选图第 4 张被阻止（最多 3 张）", () => {
    renderCreatePost();
    const input = document.querySelector<HTMLInputElement>(".img-picker-add input[type=file]")!;
    pickImages(input, 4);
    expect(document.querySelectorAll(".img-preview")).toHaveLength(3);
    expect(screen.getByText("已达 3 张上限")).toBeInTheDocument();
  });

  it("部分上传失败：可移除失败项后发布成功", async () => {
    uploadMock.mockImplementation(async (file: File) => {
      if (file.name === "p1.png") {
        throw new MemoryApiError(
          502,
          { code: "COMMUNITY_UPLOAD_FAILED", message: "x", retryable: true },
          "fallback",
        );
      }
      return {
        attachment_id: `att-${file.name}`,
        url: `/u/${file.name}`,
        mime: "image/png",
        width: 10,
        height: 10,
        size_bytes: 100,
      };
    });
    createPostMock.mockResolvedValue({} as Awaited<ReturnType<typeof createPost>>);
    const onDone = vi.fn();
    renderCreatePost({ onDone });
    fillForm();
    const input = document.querySelector<HTMLInputElement>(".img-picker-add input[type=file]")!;
    pickImages(input, 2); // p0 成功，p1 失败

    fireEvent.click(screen.getByText("发布"));
    await waitFor(() =>
      expect(screen.getByText(/部分图片上传失败/)).toBeInTheDocument(),
    );
    expect(onDone).not.toHaveBeenCalled();
    expect(createPostMock).not.toHaveBeenCalled();

    // 移除失败项（不调后端删除接口）后可发布
    const removeButtons = screen.getAllByText("移除");
    fireEvent.click(removeButtons[1]);
    fireEvent.click(screen.getByText("发布"));
    await waitFor(() => expect(onDone).toHaveBeenCalledTimes(1));
    expect(createPostMock).toHaveBeenCalledTimes(1);
    expect(createPostMock.mock.calls[0][0]["attachment_ids"]).toEqual(["att-p0.png"]);
  });

  it("发帖 422 失败保留文本与附件", async () => {
    uploadMock.mockResolvedValue({
      attachment_id: "att-1",
      url: "/u/a.png",
      mime: "image/png",
      width: 1,
      height: 1,
      size_bytes: 1,
    });
    createPostMock.mockRejectedValue(
      new MemoryApiError(
        422,
        {
          code: "COMMUNITY_CONTENT_INVALID",
          message: "标题不符合规范",
          retryable: false,
          field: "title",
        },
        "fallback",
      ),
    );
    renderCreatePost();
    fillForm();
    const input = document.querySelector<HTMLInputElement>(".img-picker-add input[type=file]")!;
    pickImages(input, 1);
    fireEvent.click(screen.getByText("发布"));
    await waitFor(() => expect(screen.getByText("标题不符合规范")).toBeInTheDocument());
    // 文本与已上传附件保留（重试不重传）
    expect(screen.getByPlaceholderText(/标题/)).toHaveValue("测试标题");
    expect(screen.getByText("已上传")).toBeInTheDocument();
  });
});

describe("帖子详情写交互", () => {
  const DETAIL = {
    ...POST,
    body: "正文",
    deleted: false,
    discussion_status: "open",
    viewer_is_author: true,
    solved_reply_id: null,
    deleted_at: null,
    attachments: [],
  };

  function mockDetail() {
    server.use(
      http.get(`*/memory-api/api/v1/community/posts/${POST.post_id}`, () =>
        HttpResponse.json({ post: DETAIL, replies: { items: [], next_cursor: null, has_more: false } }),
      ),
    );
  }

  it("点赞调用 like 接口", async () => {
    mockDetail();
    let likeCalls = 0;
    server.use(
      http.post(`*/memory-api/api/v1/community/posts/${POST.post_id}/like`, () => {
        likeCalls += 1;
        return HttpResponse.json({ status: "ok" });
      }),
    );
    render(
      <PostDetail postId={POST.post_id} onBack={() => {}} isLoggedIn={true} onLoginRequired={() => {}} />,
    );
    await waitFor(() => expect(screen.getByText("特征值直觉")).toBeInTheDocument());
    const likeButton = document.querySelector<HTMLButtonElement>(".comm-action")!;
    fireEvent.click(likeButton);
    await waitFor(() => expect(likeCalls).toBe(1));
  });

  it("删除帖子：确认弹层 → DELETE → 返回", async () => {
    mockDetail();
    let deleteCalls = 0;
    server.use(
      http.delete(`*/memory-api/api/v1/community/posts/${POST.post_id}`, () => {
        deleteCalls += 1;
        return HttpResponse.json({ status: "ok" });
      }),
    );
    const onBack = vi.fn();
    render(
      <PostDetail postId={POST.post_id} onBack={onBack} isLoggedIn={true} onLoginRequired={() => {}} />,
    );
    await waitFor(() => expect(screen.getByText("特征值直觉")).toBeInTheDocument());
    fireEvent.click(screen.getByText("删除"));
    fireEvent.click(screen.getByText("确认删除"));
    await waitFor(() => expect(deleteCalls).toBe(1));
    await waitFor(() => expect(onBack).toHaveBeenCalledTimes(1));
  });
});
