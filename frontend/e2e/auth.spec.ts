// 浏览器主链路 E2E（方案 §12 验收标准 / 评审 P2-12）：注册 → 登录 →
// 真实 memory API 200 → 刷新页面会话不丢 → 退出后旧凭据访问被拒（401）。
import { expect, test } from "@playwright/test";

test("主链路：注册 → 登录 → memory API 200 → 刷新保持会话 → 退出后 401", async ({ page }) => {
  const username = `e2e_${Date.now()}`;
  const password = "e2e-password-123";

  // 未登录 → 登录页
  await page.goto("/");
  await expect(page.getByText("欢迎回来")).toBeVisible();

  // 注册新账号
  await page.getByRole("button", { name: "注册", exact: true }).click();
  await expect(page.getByText("创建账号")).toBeVisible();
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("邮箱（选填）").fill("");
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "注册", exact: true }).click();
  await expect(page.getByText("注册成功")).toBeVisible();
  await page.getByRole("button", { name: "去登录" }).click();

  // 登录：等待一次真实 memory API 请求成功（浏览器携带真实 Bearer token）
  const memoryCall = page.waitForResponse(
    (response) => response.url().includes("/api/v1/memory/") && response.status() === 200,
  );
  await expect(page.getByText("欢迎回来")).toBeVisible();
  await page.getByLabel(/用户名 \/ 邮箱/).fill(username);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();

  // 主界面：首页 hero 问候（登录成功回到默认首页）
  await expect(page.getByText("晚上好")).toBeVisible();
  await memoryCall;

  // 刷新页面：会话不丢（静默 refresh）
  await page.reload();
  await expect(page.getByText("晚上好")).toBeVisible();

  // 退出登录：Profile 页 logout → 回到登录页
  await page.getByRole("button", { name: "个人中心" }).click();
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page.getByText("欢迎回来")).toBeVisible();

  // 评审 P2-12：退出后旧会话访问 memory API 必须被拒（401）
  const afterLogout = await page.evaluate(async () => {
    const response = await fetch("/memory-api/api/v1/memory/index");
    return response.status;
  });
  expect(afterLogout).toBe(401);
});
