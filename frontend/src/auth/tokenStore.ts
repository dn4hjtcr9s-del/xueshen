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

// ---------------------------------------------------------------------------
// 跨标签页 logout epoch（复审 P1）
//
// BroadcastChannel 消息可能因标签页尚未订阅而丢失（惰性初始化窗口），因此用
// localStorage 持久化的单调递增 epoch 标记"会话已终止"。任何 refresh 在等锁
// 期间都必须复查 epoch：发生过 logout（本地退出或 401 失效）即不得发起请求，
// 也不得广播新 access token。localStorage 与 access token 无关（方案 §9.1 仅
// 禁止持久化 token 本身），仅用于跨标签页取消标记。
// ---------------------------------------------------------------------------

const LOGOUT_EPOCH_KEY = "gewu-auth-logout-epoch";
let memoryEpoch = 0;

export function getLogoutEpoch(): number {
  if (typeof localStorage !== "undefined") {
    try {
      const raw = localStorage.getItem(LOGOUT_EPOCH_KEY);
      if (raw !== null) {
        const stored = Number(raw);
        if (Number.isFinite(stored) && stored > memoryEpoch) {
          memoryEpoch = stored;
        }
      }
    } catch {
      // localStorage 不可用（隐私模式等）：仅内存计数
    }
  }
  return memoryEpoch;
}

export function incrementLogoutEpoch(): number {
  memoryEpoch += 1;
  if (typeof localStorage !== "undefined") {
    try {
      localStorage.setItem(LOGOUT_EPOCH_KEY, String(memoryEpoch));
    } catch {
      // 忽略写入失败：内存计数仍生效
    }
  }
  return memoryEpoch;
}
