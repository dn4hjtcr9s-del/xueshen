// access token 仅存内存模块变量（方案 §9.1）：绝不写 localStorage / sessionStorage，
// 刷新页面后由静默 refresh 恢复会话。
let accessToken: string | null = null;

// 每次 setAccessToken 递增：供跨标签页 refresh 判断"等锁期间是否已有新结果"
let generation = 0;

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string | null): void {
  accessToken = token;
  generation += 1;
}

export function getAccessTokenGeneration(): number {
  return generation;
}
