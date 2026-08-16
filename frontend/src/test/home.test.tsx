// 首页新用户分流测试：访客与无学习记录账号展示引导，有真实活动后展示学习概览。
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useAuth } from "../auth/AuthContext";
import { listConversations } from "../api/conversations";
import { getMemoryIndex, getMyGraphStates } from "../api/memory";
import { HomePage } from "../pages/Home";

vi.mock("../auth/AuthContext", () => ({ useAuth: vi.fn() }));
vi.mock("../api/conversations", () => ({ listConversations: vi.fn() }));
vi.mock("../api/memory", () => ({
  getMemoryIndex: vi.fn(),
  getMyGraphStates: vi.fn(),
}));

const mockedUseAuth = vi.mocked(useAuth);
const mockedListConversations = vi.mocked(listConversations);
const mockedGetMemoryIndex = vi.mocked(getMemoryIndex);
const mockedGetMyGraphStates = vi.mocked(getMyGraphStates);

const authUser = {
  user_id: "11111111-2222-3333-4444-555555555555",
  username: "alice01",
  email: null,
  status: "active" as const,
  created_at: "2026-08-14T00:00:00Z",
};

function mockAuth(user: typeof authUser | null) {
  mockedUseAuth.mockReturnValue({
    user,
    ready: true,
    login: vi.fn(),
    logout: vi.fn(),
    logoutWarning: null,
    initials: user?.username.slice(0, 1).toUpperCase() ?? "",
  });
}

function mockNoActivity() {
  mockedListConversations.mockResolvedValue({ items: [], next_cursor: null, has_more: false });
  mockedGetMemoryIndex.mockResolvedValue({
    version: 0,
    entries: [],
    updated_at: "2026-08-14T00:00:00Z",
    stale: false,
  });
  mockedGetMyGraphStates.mockResolvedValue([]);
}

describe("首页新用户引导", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("访客看到主页首屏和主动学习模块，不展示虚构个人进度", () => {
    mockAuth(null);
    render(<HomePage goChat={vi.fn()} go={vi.fn()} />);

    expect(screen.getByRole("heading", { name: /真正的问题/ })).toBeInTheDocument();
    expect(screen.getByText("今日学习单")).toBeInTheDocument();
    expect(screen.getByText("掌握度")).toBeInTheDocument();
    expect(screen.queryByText("四步开始")).not.toBeInTheDocument();
    expect(screen.queryByText("47")).not.toBeInTheDocument();
    expect(screen.queryByText("超过 87% 的同学")).not.toBeInTheDocument();
    expect(mockedListConversations).not.toHaveBeenCalled();
  });

  it("已登录但没有学习活动时展示主动学习单并可启动任务", async () => {
    mockAuth(authUser);
    mockNoActivity();
    const goChat = vi.fn();
    render(<HomePage goChat={goChat} go={vi.fn()} />);

    expect(await screen.findByRole("heading", { name: /欢迎，alice01/ })).toBeInTheDocument();
    const taskButton = screen.getByRole("button", { name: /开始任务：用一个例子检验你的理解/ });
    await userEvent.click(taskButton);

    expect(goChat).toHaveBeenCalledWith(expect.stringContaining("检验自己对数学概念的理解"));
    expect(screen.queryByText("四步开始")).not.toBeInTheDocument();
  });

  it("存在真实会话后展示已有用户学习概览", async () => {
    mockAuth(authUser);
    mockNoActivity();
    mockedListConversations.mockResolvedValue({
      items: [
        {
          thread_id: "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
          title: "函数极限",
          status: "active",
          version: 1,
          updated_at: "2026-08-14T01:00:00Z",
        },
      ],
      next_cursor: null,
      has_more: false,
    });
    render(<HomePage goChat={vi.fn()} go={vi.fn()} />);

    await waitFor(() => expect(screen.getByText("今日任务")).toBeInTheDocument());
    expect(screen.getByRole("heading", { name: /欢迎回来，alice01/ })).toBeInTheDocument();
    expect(screen.queryByText("四步开始")).not.toBeInTheDocument();
  });
});
