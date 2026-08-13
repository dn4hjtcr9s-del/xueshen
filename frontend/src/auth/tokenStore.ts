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
//
// 单调性保证（复审 P1）：memoryEpoch 是每个 JS context 的本地计数，从 0 开始；
// 任何写入 localStorage 前必须先用存储值做 max 吸收，杜绝新标签页把全局
// epoch 回退（例如存储为 5、新标签页 memoryEpoch=0 直接 +1 写成 1）。
// ---------------------------------------------------------------------------

const LOGOUT_EPOCH_KEY = "gewu-auth-logout-epoch";
let memoryEpoch = 0;

/** localStorage 可用性探测：某些环境（如 vitest jsdom）提供空壳对象但方法缺失 */
function storageAvailable(): boolean {
  return (
    typeof localStorage !== "undefined" &&
    localStorage !== null &&
    typeof localStorage.getItem === "function" &&
    typeof localStorage.setItem === "function"
  );
}

function readStoredEpoch(): number {
  if (!storageAvailable()) return 0;
  try {
    const raw = localStorage.getItem(LOGOUT_EPOCH_KEY);
    if (raw === null) return 0;
    const stored = Number(raw);
    return Number.isFinite(stored) && stored > 0 ? stored : 0;
  } catch {
    return 0; // localStorage 不可用（隐私模式等）：仅内存计数
  }
}

function writeStoredEpoch(value: number): void {
  if (!storageAvailable()) return;
  try {
    localStorage.setItem(LOGOUT_EPOCH_KEY, String(value));
  } catch {
    // 忽略写入失败：内存计数仍生效
  }
}

export function getLogoutEpoch(): number {
  // 吸收存储值（max），保证与全局一致且单调
  memoryEpoch = Math.max(memoryEpoch, readStoredEpoch());
  return memoryEpoch;
}

export function incrementLogoutEpoch(): number {
  // 先吸收存储最大值再 +1，杜绝回退
  memoryEpoch = Math.max(memoryEpoch, readStoredEpoch()) + 1;
  writeStoredEpoch(memoryEpoch);
  return memoryEpoch;
}

/** 接收端采用 epoch（复审 P1）：只取 max，绝不自行递增，避免回退全局计数。 */
export function adoptLogoutEpoch(candidate: number): number {
  memoryEpoch = Math.max(memoryEpoch, readStoredEpoch(), candidate);
  writeStoredEpoch(memoryEpoch);
  return memoryEpoch;
}
