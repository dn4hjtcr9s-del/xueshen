// access token 仅存内存模块变量（方案 §9.1）：绝不写 localStorage / sessionStorage，
// 刷新页面后由静默 refresh 恢复会话。
let accessToken: string | null = null;

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string | null): void {
  accessToken = token;
}
