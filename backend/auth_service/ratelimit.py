"""认证端点限流与客户端地址解析（方案 §10.1 / 附录 A.2 #9）。

- 阈值实现为常量（附录 A.3 #13），不进 settings。
- 客户端 IP 只信任已配置代理来源（AUTH_TRUSTED_PROXY_CIDRS）提供的
  X-Forwarded-For；默认空 = 仅信任直连地址。
- v1.6（D24/D18）：client_ip 实现已提取至 backend/shared/client_ip.py，
  Community 与 Auth 共用同一可信代理解析算法，此处保留 re-export。
"""

from __future__ import annotations

import time

from backend.shared.client_ip import client_ip as client_ip  # D24 re-export

#: 每窗口（分钟）限流阈值常量（方案 §10.1）
LOGIN_FAIL_PER_IP = 10
LOGIN_FAIL_PER_ACCOUNT = 5
REGISTER_PER_IP = 5
REFRESH_PER_IP = 30

#: 限流窗口长度（秒）；与 FixedWindowRateLimiter 的分钟窗口一致
_WINDOW_SECONDS = 60


def retry_after_seconds() -> int:
    """429 Retry-After：当前分钟窗口剩余秒数。"""
    return max(1, _WINDOW_SECONDS - int(time.time() % _WINDOW_SECONDS))
