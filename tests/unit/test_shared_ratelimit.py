"""backend/shared/ratelimit.py 共享化回归测试（D12/D19/D24）。

- 默认 60 秒窗口语义与既有调用兼容（位置参数调用不受影响）；
- D12：window_seconds 小时级窗口（3600）；
- D19：墙钟固定窗口对齐、边界突发语义、Retry-After 按窗口剩余秒数；
- 计数器 key 含 (bucket, principal, window_seconds)，分钟/小时计数互不碰撞；
- 过期清理按每个 counter 自己的窗口编号判断。
"""

from __future__ import annotations

from unittest.mock import patch

from backend.shared.ratelimit import FixedWindowRateLimiter, retry_after_seconds


def _at(epoch: float):
    return patch("backend.shared.ratelimit.time.time", return_value=epoch)


def test_default_window_60_seconds_compatible() -> None:
    """D12：默认 window_seconds=60，既有调用语义不变。"""
    limiter = FixedWindowRateLimiter()
    with _at(1_700_000_000.0):
        assert limiter.hit("write", "user-1", limit=2)
        assert limiter.hit("write", "user-1", limit=2)
        assert not limiter.hit("write", "user-1", limit=2)
        assert limiter.is_limited("write", "user-1", limit=2)


def test_clear_resets_bucket() -> None:
    limiter = FixedWindowRateLimiter()
    with _at(1_700_000_000.0):
        limiter.hit("write", "user-1", limit=1)
        assert limiter.is_limited("write", "user-1", limit=1)
        limiter.clear("write", "user-1")
        assert not limiter.is_limited("write", "user-1", limit=1)


def test_hour_window_wall_clock_alignment() -> None:
    """D19：3600 秒窗口按 Unix 小时对齐；跨小时边界各得一段额度。"""
    limiter = FixedWindowRateLimiter()
    hour_start = 1_700_000_000.0  # 对齐到小时整点
    with _at(hour_start):
        assert limiter.hit("community.post.create", "u1", limit=10, window_seconds=3600)
    with _at(hour_start + 3599):
        assert limiter.hit("community.post.create", "u1", limit=10, window_seconds=3600)
    # 下一小时窗口：额度重新计算（D19 边界突发可接受）
    with _at(hour_start + 3600):
        assert limiter.hit("community.post.create", "u1", limit=10, window_seconds=3600)


def test_hour_window_limit_enforced() -> None:
    limiter = FixedWindowRateLimiter()
    with _at(1_700_000_000.0):
        for _ in range(10):
            assert limiter.hit("community.post.create", "u1", limit=10, window_seconds=3600)
        assert not limiter.hit("community.post.create", "u1", limit=10, window_seconds=3600)


def test_minute_and_hour_buckets_do_not_collide() -> None:
    """D19：同名 bucket 的分钟/小时计数使用不同 key，互不影响。"""
    limiter = FixedWindowRateLimiter()
    with _at(1_700_000_000.0):
        # 分钟桶连续 5 次打满
        for _ in range(5):
            assert limiter.hit("community.reply.create", "u1", limit=5, window_seconds=60)
        # 小时桶在同一时刻仍是独立计数
        assert limiter.hit("community.reply.create", "u1", limit=60, window_seconds=3600)


def test_prune_keeps_entries_by_own_window() -> None:
    """容量保护按每个 counter 自己的窗口编号清理（不是单一 60 秒推断）。"""
    limiter = FixedWindowRateLimiter()
    now = 1_700_000_000.0
    # 预置大量已过期的 60s 条目（30 分钟前）与一条上一小时的 3600s 条目
    old_minute = now - 30 * 60
    old_hour = now - 3600
    for i in range(10_001):
        key = (f"bucket-{i}", "u1", 60)
        limiter._counters[key] = (int(old_minute // 60), 1)
    hour_key = ("h", "u1", 3600)
    limiter._counters[hour_key] = (int(old_hour // 3600), 1)
    with _at(now):
        limiter._prune()
    # 30 分钟前的 60s 条目已清理；上一小时的 3600s 条目窗口编号未过期被保留
    assert all(not k.startswith("bucket") for k, _, _ in limiter._counters)
    assert hour_key in limiter._counters


def test_retry_after_seconds_scales_with_window() -> None:
    """D19：Retry-After 按窗口剩余秒数，不是固定 60。"""
    # 1_728_000_000 为 3600 的整倍数（= 3600 * 480000），对齐小时窗口
    hour_start = 1_728_000_000.0
    with _at(hour_start + 120):
        assert retry_after_seconds(3600) == 3600 - 120
    with _at(hour_start):
        assert retry_after_seconds(60) == 60
