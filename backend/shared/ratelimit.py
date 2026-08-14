"""跨域共享：进程内固定窗口限流器（方案 §18.5 / D12 / D19 / D24）。

从 backend/memory/api/dependencies.py:281 提取，Memory 侧原位置保留 re-export。
v1.6 扩展（D12）：_current/hit/is_limited/clear 增加 window_seconds 参数，
默认 60 保持既有调用语义不变；Community 小时级规则传 3600。

窗口语义（D19）：按 Unix 时间戳对齐墙钟的固定窗口
window_id = floor(time.time() / window_seconds)，不是滑动窗口/漏桶/令牌桶，
边界附近允许短时耗尽相邻两段额度。计数器 key 包含 (bucket, principal,
window_seconds)，同名 bucket 的分钟/小时计数互不碰撞；过期清理按每个
counter 自己的窗口编号判断。
"""

from __future__ import annotations

import time


class FixedWindowRateLimiter:
    """进程内固定窗口限流器（单实例 best-effort，不宣称多副本全局精确）。"""

    def __init__(self) -> None:
        # key=(bucket, principal, window_seconds) → (window_id, count)
        self._counters: dict[tuple[str, str, int], tuple[int, int]] = {}

    def _current(self, bucket: str, principal: str, window_seconds: int) -> tuple[int, int]:
        window = int(time.time() // window_seconds)
        key = (bucket, principal, window_seconds)
        stored_window, count = self._counters.get(key, (window, 0))
        if stored_window != window:
            stored_window, count = window, 0
        return stored_window, count

    def _prune(self) -> None:
        """容量保护：按每个 counter 自己的窗口编号清理过期条目。

        保留最近一个完整窗口（含当前窗口编号），窗口秒数不同也各自正确
        （例如 3600 秒窗口在 60 秒窗口清理时不会被误删）。
        """
        now = time.time()
        self._counters = {
            key: value
            for key, value in self._counters.items()
            if value[0] >= int(now // key[2]) - 1
        }

    def hit(
        self,
        bucket: str,
        principal: str,
        limit: int,
        window_seconds: int = 60,
    ) -> bool:
        """计数并返回是否仍在限额内（超过返回 False，仍会记录本次计数）。"""
        window, count = self._current(bucket, principal, window_seconds)
        count += 1
        self._counters[(bucket, principal, window_seconds)] = (window, count)
        if len(self._counters) > 10_000:
            self._prune()
        return count <= limit

    def is_limited(
        self,
        bucket: str,
        principal: str,
        limit: int,
        window_seconds: int = 60,
    ) -> bool:
        """只读探测是否已超限（不计数），供预检避免昂贵操作。"""
        _, count = self._current(bucket, principal, window_seconds)
        return count >= limit

    def clear(self, bucket: str, principal: str, window_seconds: int = 60) -> None:
        """清除特定窗口桶计数（如成功登录清除账号桶）。"""
        self._counters.pop((bucket, principal, window_seconds), None)


def retry_after_seconds(window_seconds: int) -> int:
    """429 Retry-After：当前窗口剩余秒数（D19：不固定为 60）。"""
    return max(1, window_seconds - int(time.time() % window_seconds))
