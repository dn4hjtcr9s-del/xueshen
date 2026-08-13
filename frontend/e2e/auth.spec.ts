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
