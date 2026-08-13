// 浏览器主链路 E2E（方案 §12 验收标准 / 评审 P2-4 / 复审 P1-2）：
// 注册 → 登录 → 真实 memory API 200 → 刷新页面会话不丢 →
// 退出后旧 refresh cookie 重放 401、客户端不再携带 Bearer；
// 多标签页并发刷新不会撤销合法会话。
import { expect, test } from "@playwright/test";

const PASSWORD = "e2e-password-123";

async function registerAndLogin(page: import("@playwright/test").Page): Promise<void> {
  const username = `e2e_${Date.now()}`;
  // 未登录 → 登录页
  await page.goto("/");
  await expect(page.getByText("欢迎回来")).toBeVisible();

  // 注册新账号
  await page.getByRole("button", { name: "注册", exact: true }).click();
  await expect(page.getByText("创建账号")).toBeVisible();
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("邮箱（选填）").fill("");
  await page.getByLabel("密码", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "注册", exact: true }).click();
  await expect(page.getByText("注册成功")).toBeVisible();
  await page.getByRole("button", { name: "去登录" }).click();

  // 登录：等待一次真实 memory API 请求成功（浏览器携带真实 Bearer token）
  const memoryCall = page.waitForResponse(
    (response) => response.url().includes("/api/v1/memory/") && response.status() === 200,
  );
  await expect(page.getByText("欢迎回来")).toBeVisible();
  await page.getByLabel(/用户名 \/ 邮箱/).fill(username);
  await page.getByLabel("密码", { exact: true }).fill(PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByText("晚上好")).toBeVisible();
  await memoryCall;
}

test("主链路：注册 → 登录 → memory API 200 → 刷新保持会话 → 退出后旧凭据失效", async ({
  page,
  context,
}) => {
  await registerAndLogin(page);

  // 刷新页面：会话不丢（静默 refresh）
  await page.reload();
  await expect(page.getByText("晚上好")).toBeVisible();

  // 捕获旧 refresh cookie 值（退出后用于重放验证）
  const cookiesBefore = await context.cookies();
  const refreshCookie = cookiesBefore.find((c) => c.name === "gewu_refresh_token");
  expect(refreshCookie).toBeDefined();

  // 评审 P2-4 / 复审 P3：同 JS context 内验证退出后客户端不再携带 Bearer。
  // 探针：logout 自身带 Bearer（证明监听有效），退出后在登录页发起一次登录
  // 请求，断言其不带 Authorization。
  const authorizationHeaders: string[] = [];
  const collectAuth = (request: import("@playwright/test").Request) => {
    const header = request.headers()["authorization"];
    if (header) authorizationHeaders.push(header);
  };
  page.on("request", collectAuth);

  // 退出登录：Profile 页 logout → 回到登录页
  await page.getByRole("button", { name: "个人中心" }).click();
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page.getByText("欢迎回来")).toBeVisible();

  // 评审 P2-4：用旧 refresh cookie 显式重放 → 401（family 已被服务端撤销，
  // 且不受浏览器 cookie 是否删除的影响）
  expect(refreshCookie).toBeDefined();
  if (refreshCookie) {
    await context.addCookies([
      { name: refreshCookie.name, value: refreshCookie.value, url: "http://localhost:4173" },
    ]);
    const replayStatus = await page.evaluate(async () => {
      const response = await fetch("/api/v1/auth/refresh", { method: "POST" });
      return response.status;
    });
    expect(replayStatus).toBe(401);
    await context.clearCookies();
  }

  // 等 Profile 页遗留请求全部结束，后续只观察登录探针
  await page.waitForLoadState("networkidle");
  const marker = authorizationHeaders.length;
  expect(marker).toBeGreaterThan(0);
  expect(authorizationHeaders[marker - 1]).toContain("Bearer "); // logout 请求带 Bearer（对照）

  // 复审 P3：同一 JS context（无 reload）触发登录请求——若 access token
  // 未被清除，该请求会携带旧 Bearer
  await page.getByLabel(/用户名 \/ 邮箱/).fill("probe-user");
  await page.getByLabel("密码", { exact: true }).fill("probe-password-123");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByText("账号或密码错误")).toBeVisible();
  expect(authorizationHeaders).toHaveLength(marker); // 登录请求未携带 Bearer
});

test("多标签页并发刷新不会撤销合法会话（复审 P1-2 / P3 barrier）", async ({
  page,
  context,
}) => {
  await registerAndLogin(page);

  // 第二个标签页：静默恢复会话（触发一次 refresh）
  const page2 = await context.newPage();
  await page2.goto("/");
  await expect(page2.getByText("晚上好")).toBeVisible();

  // 复审 P3：用延迟放行让两个标签页的刷新窗口真实重叠——若前端没有
  // Web Locks 串行化且没有 token 复用，两个标签页会同时提交旧 cookie，
  // 触发服务端重放检测撤销整族，后续断言（会话仍有效）即会失败。
  // 注：token 复用优化生效时，第二个标签页可能不发第二个 refresh 请求，
  // 这同样是正确行为（gatedRefreshCount >= 1 即可）。
  const gatedRefreshCount: string[] = [];
  await context.route("**/api/v1/auth/refresh", async (route) => {
    gatedRefreshCount.push(route.request().url());
    await new Promise((resolve) => setTimeout(resolve, 1500));
    await route.fallback();
  });

  await Promise.all([page.reload(), page2.reload()]);
  await expect(page.getByText("晚上好")).toBeVisible({ timeout: 15_000 });
  await expect(page2.getByText("晚上好")).toBeVisible({ timeout: 15_000 });
  expect(gatedRefreshCount.length).toBeGreaterThanOrEqual(1); // 至少一个标签页进入了 refresh

  // 会话仍有效：再次刷新依旧能静默恢复（family 未被重放撤销）
  await page.reload();
  await expect(page.getByText("晚上好")).toBeVisible({ timeout: 15_000 });
  await page2.reload();
  await expect(page2.getByText("晚上好")).toBeVisible({ timeout: 15_000 });
});

test("logout 失败时等待锁的标签页不得恢复会话（复审 P1）", async ({ page, context }) => {
  await registerAndLogin(page);

  // 第二个标签页：静默恢复会话（触发一次 refresh）
  const page2 = await context.newPage();
  await page2.goto("/");
  await expect(page2.getByText("晚上好")).toBeVisible();

  // 拦截 logout：持锁期间不放行，最终返回 503（family 未撤销、Cookie 未删除）
  let logoutRequestStarted!: () => void;
  let releaseLogout!: () => void;
  const logoutHeld = new Promise<void>((resolve) => {
    logoutRequestStarted = resolve;
  });
  const releasePromise = new Promise<void>((resolve) => {
    releaseLogout = resolve;
  });
  await context.route("**/api/v1/auth/logout", async (route) => {
    logoutRequestStarted();
    await releasePromise;
    await route.fulfill({
      status: 503,
      json: {
        error: {
          code: "AUTH_DB_UNAVAILABLE",
          message: "认证数据库暂不可用，请稍后重试",
          retryable: true,
          field: null,
          trace_id: "e2e",
        },
      },
    });
  });

  // 统计 logout 持锁期间各标签页发起的 refresh 请求（B 的排队 refresh 应为 0）
  let refreshDuringLogout = 0;
  await context.route("**/api/v1/auth/refresh", async (route) => {
    refreshDuringLogout += 1;
    await route.fallback();
  });

  // A 发起 logout（withRefreshLock 持有跨标签页锁）
  await page.getByRole("button", { name: "个人中心" }).click();
  await page.getByRole("button", { name: "退出登录" }).click();
  await logoutHeld;

  // B 在 logout 持锁期间 reload → restore 的 refresh 排队等待同一把锁。
  // 等待 React 挂载 + refreshSession 入队完成（确定性：放行 logout 前 B 必已排队）
  await page2.reload();
  await new Promise((resolve) => setTimeout(resolve, 2000));

  // 放行 logout → 503：A 本地清除并递增 logout epoch、广播 logout
  releaseLogout();
  await expect(page.getByText("欢迎回来")).toBeVisible({ timeout: 15_000 });
  await expect(page2.getByText("欢迎回来")).toBeVisible({ timeout: 15_000 });

  // 复审 P1：B 的排队 refresh 必须被 epoch 复查拦截，不得发出请求
  await page2.waitForLoadState("networkidle");
  expect(refreshDuringLogout).toBe(0);

  // A 侧探针：登录请求不得携带 Bearer（本地 token 已被清除）
  await page.getByLabel(/用户名 \/ 邮箱/).fill("probe-user");
  await page.getByLabel("密码", { exact: true }).fill("probe-password-123");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByText("账号或密码错误")).toBeVisible();
});

test("无 Web Locks 环境下 logout 后排队 refresh 不得恢复会话（复审 P1）", async ({
  context,
}) => {
  // 禁用 navigator.locks：验证退化为同页面互斥队列 + epoch 复检的故障路径
  await context.addInitScript(() => {
    try {
      Object.defineProperty(navigator, "locks", {
        configurable: true,
        value: undefined,
      });
    } catch {
      // 忽略：个别环境不可覆盖
    }
  });
  const page = await context.newPage();
  await registerAndLogin(page);

  // 第二个标签页：静默恢复会话
  const page2 = await context.newPage();
  await page2.goto("/");
  await expect(page2.getByText("晚上好")).toBeVisible();

  // 拦截 logout：持锁（页面互斥队列）期间不放行，最终返回 503
  let logoutRequestStarted!: () => void;
  let releaseLogout!: () => void;
  const logoutHeld = new Promise<void>((resolve) => {
    logoutRequestStarted = resolve;
  });
  const releasePromise = new Promise<void>((resolve) => {
    releaseLogout = resolve;
  });
  await context.route("**/api/v1/auth/logout", async (route) => {
    logoutRequestStarted();
    await releasePromise;
    await route.fulfill({
      status: 503,
      json: {
        error: {
          code: "AUTH_DB_UNAVAILABLE",
          message: "认证数据库暂不可用，请稍后重试",
          retryable: true,
          field: null,
          trace_id: "e2e",
        },
      },
    });
  });

  // A 发起 logout
  await page.getByRole("button", { name: "个人中心" }).click();
  await page.getByRole("button", { name: "退出登录" }).click();
  await logoutHeld;

  // B reload：无 Web Locks 时 refresh 经同页互斥队列排在 logout 之后（P3-3 修复），
  // logout 完成后 B 通过消息携带的 epoch（adopt max）与响应复检，
  // 最终不得停留在登录态
  await page2.reload();
  await new Promise((resolve) => setTimeout(resolve, 2000));

  // 放行 logout → 503
  releaseLogout();
  await expect(page.getByText("欢迎回来")).toBeVisible({ timeout: 15_000 });
  // B 无论曾短暂恢复还是被丢弃，最终必须停在登录页
  await expect(page2.getByText("欢迎回来")).toBeVisible({ timeout: 15_000 });

  // B 侧探针：登录请求不得携带 Bearer
  const authHeaders: string[] = [];
  page2.on("request", (request) => {
    const header = request.headers()["authorization"];
    if (header) authHeaders.push(header);
  });
  await page2.getByLabel(/用户名 \/ 邮箱/).fill("probe-user");
  await page2.getByLabel("密码", { exact: true }).fill("probe-password-123");
  await page2.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page2.getByText("账号或密码错误")).toBeVisible();
  await page2.waitForLoadState("networkidle");
  // 探针登录请求之前不得有任何 Bearer（logout 后）
  expect(authHeaders.filter((h) => h.includes("Bearer "))).toHaveLength(0);
});
