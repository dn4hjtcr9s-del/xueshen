"""认证端点限流与客户端地址解析（方案 §10.1 / 附录 A.2 #9）。

- 阈值实现为常量（附录 A.3 #13），不进 settings。
- 客户端 IP 只信任已配置代理来源（AUTH_TRUSTED_PROXY_CIDRS）提供的
  X-Forwarded-For；默认空 = 仅信任直连地址。
"""

from __future__ import annotations

import ipaddress
import time

from fastapi import Request

#: 每窗口（分钟）限流阈值常量（方案 §10.1）
LOGIN_FAIL_PER_IP = 10
LOGIN_FAIL_PER_ACCOUNT = 5
REGISTER_PER_IP = 5
REFRESH_PER_IP = 30

#: 限流窗口长度（秒）；与 FixedWindowRateLimiter 的分钟窗口一致
_WINDOW_SECONDS = 60


def client_ip(request: Request, trusted_proxy_cidrs: list[str]) -> str | None:
    """解析客户端 IP（附录 A.2 #9）。

    直连对端位于可信代理 CIDR 内时，采用 X-Forwarded-For 的首个地址；
    否则仅信任直连地址（忽略伪造转发头）。
    """
    direct = request.client.host if request.client else None
    if not trusted_proxy_cidrs or direct is None:
        return direct
    try:
        direct_ip = ipaddress.ip_address(direct)
    except ValueError:
        return direct
    try:
        networks = [ipaddress.ip_network(cidr.strip()) for cidr in trusted_proxy_cidrs]
    except ValueError:
        return direct
    if not any(direct_ip in net for net in networks):
        return direct
    xff = request.headers.get("x-forwarded-for")
    if not xff:
        return direct
    first = xff.split(",")[0].strip()
    try:
        ipaddress.ip_address(first)
    except ValueError:
        return direct
    return first


def retry_after_seconds() -> int:
    """429 Retry-After：当前分钟窗口剩余秒数。"""
    return max(1, _WINDOW_SECONDS - int(time.time() % _WINDOW_SECONDS))
