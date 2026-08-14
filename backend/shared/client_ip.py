"""跨域共享：可信代理客户端 IP 解析（方案 §10.1 / D18 / D24）。

从 backend/auth_service/ratelimit.py::client_ip 提取，Auth 侧保留 re-export。
Community 使用独立 COMMUNITY_TRUSTED_PROXY_CIDRS 配置，默认空 = 只信任直连对端。
"""

from __future__ import annotations

import ipaddress

from fastapi import Request


def client_ip(request: Request, trusted_proxy_cidrs: list[str]) -> str | None:
    """解析客户端 IP（附录 A.2 #9 / 评审 P1-7，D18 共享实现）。

    直连对端位于可信代理 CIDR 内时，从右向左剥离可信代理（X-Forwarded-For
    链尾追加直连对端），返回**第一个不可信地址**。攻击者伪造的 XFF 前缀位于
    链左侧，不会影响限流 key；整链均为可信代理时回退直连对端。
    非法链路 fail closed 回退直连对端；request.client 缺失时返回 None。
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
    chain: list[str] = [entry.strip() for entry in xff.split(",") if entry.strip()]
    chain.append(direct)
    for entry in reversed(chain):
        try:
            candidate = ipaddress.ip_address(entry)
        except ValueError:
            # 无法解析的链节点不可信：fail-closed 回退直连对端
            return direct
        if not any(candidate in net for net in networks):
            return str(candidate)
    # 整链都是可信代理：无可信客户端信息，回退直连对端
    return direct
