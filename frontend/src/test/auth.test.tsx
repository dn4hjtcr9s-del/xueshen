// 认证前端测试（方案 §11 / §9）：登录/注册交互、访客浏览、401 → single-flight refresh。
import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuthProvider, useAuth } from "../auth/AuthContext";
import { LoginPage } from "../pages/Login";
import { RegisterPage } from "../pages/Register";
import { ProfilePage } from "../pages/Profile";
import { request } from "../api/client";
import { setAccessToken, getAccessToken } from "../auth/tokenStore";
import { server } from "./server";
import { candidateView, deletedItem, indexView, learnerView, masteryView } from "./fixtures";

// 默认无会话：refresh 401 → 未登录
function installNoSession() {
  server.use(http.post("*/api/v1/auth/refresh", () => new HttpResponse(null, { status: 401 })));
}

function installLogin(username = "alice01") {
  server.use(
    http.post("*/api/v1/auth/login", () =>
      HttpResponse.json({
        access_token: "test-access-token",
        token_type: "bearer",
        expires_in: 300,
        user: {
          user_id: "00000000-0000-0000-0000-000000000a01",
          username,
          email: null,
          status: "active",
          created_at: "2026-08-13T00:00:00+00:00",
        },
      }),
    ),
  );
}

// 观察当前登录态（AuthProvider 不渲染 UI，登录结果需由消费组件呈现）
function UserProbe() {
  const { user } = useAuth();
  return <div>{user ? `已登录:${user.username}` : "未登录"}</div>;
}

// 登录表单 + 登录态展示（登录成功后同屏看到已登录标记）
function AuthWorkspace() {
  const { user } = useAuth();
  return (
    <div>
      <UserProbe />
      {user === null && <LoginPage onGoRegister={() => {}} />}
    </div>
  );
}

describe("登录/注册表单", () => {
  beforeEach(installNoSession);

  it("未登录展示登录表单，登录成功后进入已登录状态", async () => {
    installLogin();
    render(
      <AuthProvider>
        <AuthWorkspace />
      </AuthProvider>,
    );
    expect(await screen.findByText("未登录")).toBeInTheDocument();
    expect(screen.getByText("欢迎回来")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText(/用户名 \/ 邮箱/), "alice01");
    await userEvent.type(screen.getByLabelText("密码"), "password123");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByText("已登录:alice01")).toBeInTheDocument();
    expect(getAccessToken()).toBe("test-access-token");
  });

  it("登录失败展示服务端错误信息", async () => {
    server.use(
      http.post("*/api/v1/auth/login", () =>
        HttpResponse.json(
          {
            error: {
              code: "AUTH_INVALID_CREDENTIALS",
              message: "账号或密码错误",
              retryable: false,
              field: null,
              trace_id: "t1",
            },
          },
          { status: 401 },
        ),
      ),
    );
    render(
      <AuthProvider>
        <LoginPage onGoRegister={() => {}} />
      </AuthProvider>,
    );
    await screen.findByRole("button", { name: "登录" });
    await userEvent.type(screen.getByLabelText(/用户名 \/ 邮箱/), "alice01");
    await userEvent.type(screen.getByLabelText("密码"), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByText("账号或密码错误")).toBeInTheDocument();
  });

  it("注册成功跳转登录页（不自动登录）", async () => {
    server.use(
      http.post("*/api/v1/auth/register", () =>
        HttpResponse.json(
          {
            user: {
              user_id: "00000000-0000-0000-0000-000000000a02",
              username: "bob01",
              email: null,
              status: "active",
              created_at: "2026-08-13T00:00:00+00:00",
            },
          },
          { status: 201 },
        ),
      ),
    );
    function RegisterWorkspace() {
      const [mode, setMode] = useState<"login" | "register">("login");
      return mode === "login" ? (
        <LoginPage onGoRegister={() => setMode("register")} />
      ) : (
        <RegisterPage onGoLogin={() => setMode("login")} />
      );
    }
    render(
      <AuthProvider>
        <RegisterWorkspace />
      </AuthProvider>,
    );
    await screen.findByRole("button", { name: "登录" });
    await userEvent.click(screen.getByRole("button", { name: "注册" }));
    expect(await screen.findByText("创建账号")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("用户名"), "bob01");
    await userEvent.type(screen.getByLabelText("密码"), "password123");
    await userEvent.click(screen.getByRole("button", { name: "注册" }));

    expect(await screen.findByText("注册成功")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "去登录" }));
    expect(await screen.findByText("欢迎回来")).toBeInTheDocument();
  });
});

describe("个人中心：访客与已登录状态", () => {
  beforeEach(installNoSession);

  it("访客进入个人中心展示登录表单，可切换到注册", async () => {
    render(
      <AuthProvider>
        <ProfilePage />
      </AuthProvider>,
    );
    expect(await screen.findByText("欢迎回来")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "注册" }));
    expect(await screen.findByText("创建账号")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByText("欢迎回来")).toBeInTheDocument();
  });

  it("登录后展示已登录视图与退出按钮", async () => {
    installLogin();
    // 已登录视图会挂载 MemorySection（读取记忆数据）
    server.use(
      http.get("*/api/v1/memory/learner", () => HttpResponse.json(learnerView())),
      http.get("*/api/v1/memory/index", () => HttpResponse.json(indexView())),
      http.get("*/api/v1/memory/deleted", () =>
        HttpResponse.json({ items: [deletedItem()], next_cursor: null, has_more: false }),
      ),
      http.get("*/api/v1/memory/review-candidates", () =>
        HttpResponse.json({ items: [candidateView()], next_cursor: null, has_more: false }),
      ),
      http.get("*/api/v1/memory/mastery/:topicKey", () => HttpResponse.json(masteryView())),
    );
    render(
      <AuthProvider>
        <ProfilePage />
      </AuthProvider>,
    );
    await screen.findByText("欢迎回来");
    await userEvent.type(screen.getByLabelText(/用户名 \/ 邮箱/), "alice01");
    await userEvent.type(screen.getByLabelText("密码"), "password123");
    await userEvent.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByText("alice01")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "退出登录" })).toBeInTheDocument();
    expect(screen.queryByText("欢迎回来")).not.toBeInTheDocument();
  });
});

describe("共享请求层 401 → single-flight refresh（§9.3）", () => {
  it("并发 401 只触发一次 refresh，随后重放原请求成功", async () => {
    let refreshCalls = 0;
    let indexCalls = 0;
    setAccessToken(null);
    server.use(
      http.post("*/api/v1/auth/refresh", () => {
        refreshCalls += 1;
        return HttpResponse.json({ access_token: "refreshed-token" });
      }),
      http.get("*/api/v1/memory/index", ({ request }) => {
        indexCalls += 1;
        if (request.headers.get("Authorization") === "Bearer refreshed-token") {
          return HttpResponse.json({ version: 1, entries: [], updated_at: null, stale: false });
        }
        return new HttpResponse(null, { status: 401 });
      }),
    );

    const [a, b] = await Promise.all([
      request("GET", "/memory/index"),
      request("GET", "/memory/index"),
    ]);
    expect(a).toBeDefined();
    expect(b).toBeDefined();
    expect(refreshCalls).toBe(1);
    expect(indexCalls).toBe(4); // 2 次 401 + 2 次重放
    expect(getAccessToken()).toBe("refreshed-token");
  });

  it("refresh 失败时清空 token 并保留原始 401 错误", async () => {
    setAccessToken("stale-token");
    server.use(
      http.post("*/api/v1/auth/refresh", () => new HttpResponse(null, { status: 401 })),
      http.get("*/api/v1/memory/index", () => new HttpResponse(null, { status: 401 })),
    );
    await expect(request("GET", "/memory/index")).rejects.toMatchObject({ status: 401 });
    expect(getAccessToken()).toBeNull();
  });

  it("logout 服务端失败仍清除本地 token（评审 P1-3）", async () => {
    const { logout } = await import("../api/auth");
    setAccessToken("local-token");
    server.use(
      http.post("*/api/v1/auth/logout", () => new HttpResponse(null, { status: 503 })),
    );
    await expect(logout()).rejects.toMatchObject({ status: 503 });
    expect(getAccessToken()).toBeNull();
  });

  it("refresh 遇 503 视为可重试故障，不清 token（复审 P2）", async () => {
    setAccessToken("valid-token");
    server.use(
      http.post("*/api/v1/auth/refresh", () => new HttpResponse(null, { status: 503 })),
      http.get("*/api/v1/memory/index", () => new HttpResponse(null, { status: 401 })),
    );
    await expect(request("GET", "/memory/index")).rejects.toMatchObject({ status: 401 });
    expect(getAccessToken()).toBe("valid-token");
  });

  it("refresh 网络故障不清 token（复审 P2）", async () => {
    setAccessToken("valid-token");
    server.use(
      http.post("*/api/v1/auth/refresh", () => HttpResponse.error()),
      http.get("*/api/v1/memory/index", () => new HttpResponse(null, { status: 401 })),
    );
    await expect(request("GET", "/memory/index")).rejects.toMatchObject({ status: 401 });
    expect(getAccessToken()).toBe("valid-token");
  });
});

describe("logout epoch 单调性（复审 P1）", () => {
  const EPOCH_KEY = "gewu-auth-logout-epoch";

  // vitest jsdom 的 localStorage 是空壳（方法缺失）：注入功能性实现供测试使用
  beforeEach(() => {
    const backing = new Map<string, string>();
    Object.defineProperty(globalThis, "localStorage", {
      configurable: true,
      value: {
        getItem: (key: string) => backing.get(key) ?? null,
        setItem: (key: string, value: string) => {
          backing.set(key, String(value));
        },
        removeItem: (key: string) => {
          backing.delete(key);
        },
        clear: () => {
          backing.clear();
        },
      },
    });
  });

  async function freshTokenStore() {
    vi.resetModules();
    return await import("../auth/tokenStore");
  }

  it("新标签页（memoryEpoch=0）递增前吸收存储值，不回退全局 epoch", async () => {
    localStorage.setItem(EPOCH_KEY, "5");
    const store = await freshTokenStore();
    // 首次读取即吸收存储值
    expect(store.getLogoutEpoch()).toBe(5);
    // 递增基于吸收后的值：5 → 6（而非 0 → 1 回退）
    expect(store.incrementLogoutEpoch()).toBe(6);
    expect(localStorage.getItem(EPOCH_KEY)).toBe("6");
  });

  it("接收端 adopt 只取 max，不自行递增", async () => {
    localStorage.setItem(EPOCH_KEY, "5");
    const store = await freshTokenStore();
    // 收到低于当前值的消息：保持 5
    expect(store.adoptLogoutEpoch(3)).toBe(5);
    // 收到高于当前值的消息：采用 7
    expect(store.adoptLogoutEpoch(7)).toBe(7);
    expect(localStorage.getItem(EPOCH_KEY)).toBe("7");
  });
});

describe("refresh 响应后 epoch 复检（复审 P1）", () => {
  it("refresh 在途时发生 logout，200 响应被丢弃，不写 token 不广播", async () => {
    setAccessToken(null);
    let refreshEntered!: () => void;
    let releaseRefresh!: () => void;
    const entered = new Promise<void>((resolve) => {
      refreshEntered = resolve;
    });
    const release = new Promise<void>((resolve) => {
      releaseRefresh = resolve;
    });
    server.use(
      http.post("*/api/v1/auth/refresh", async () => {
        refreshEntered();
        await release;
        return HttpResponse.json({ access_token: "late-token" });
      }),
      http.get("*/api/v1/memory/index", () => new HttpResponse(null, { status: 401 })),
    );

    const pending = request("GET", "/memory/index");
    await entered; // refresh 已在途（请求被延迟）
    // 模拟其他标签页 logout 完成：epoch 递增（等价于收到 logout 消息后 adopt）
    const { incrementLogoutEpoch, adoptLogoutEpoch } = await import("../auth/tokenStore");
    adoptLogoutEpoch(incrementLogoutEpoch());
    releaseRefresh(); // 放行 200

    await expect(pending).rejects.toMatchObject({ status: 401 });
    expect(getAccessToken()).toBeNull(); // 迟到的 200 被丢弃
  });
});
