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

  // 评审 P2-4：退出后重新加载页面，客户端不得再携带旧 Bearer（token 已清除）
  const authorizationHeaders: string[] = [];
  const collectAuth = (request: import("@playwright/test").Request) => {
    const header = request.headers()["authorization"];
    if (header) authorizationHeaders.push(header);
  };
  page.on("request", collectAuth);
  await page.reload();
  await expect(page.getByText("欢迎回来")).toBeVisible();
  expect(authorizationHeaders).toHaveLength(0);
});

test("多标签页并发刷新不会撤销合法会话（复审 P1-2）", async ({ page, context }) => {
  await registerAndLogin(page);

  // 第二个标签页：静默恢复会话（触发一次 refresh）
  const page2 = await context.newPage();
  await page2.goto("/");
  await expect(page2.getByText("晚上好")).toBeVisible();

  // 两个标签页同时刷新：并发 restore → refresh 经 Web Locks 串行化，
  // 不得触发重放检测导致整族撤销
  await Promise.all([page.reload(), page2.reload()]);
  await expect(page.getByText("晚上好")).toBeVisible();
  await expect(page2.getByText("晚上好")).toBeVisible();

  // 会话仍有效：再次刷新依旧能静默恢复
  await page.reload();
  await expect(page.getByText("晚上好")).toBeVisible();
  await page2.reload();
  await expect(page2.getByText("晚上好")).toBeVisible();
});
