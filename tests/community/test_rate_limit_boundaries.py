"""Community 限流依赖边界测试（§9.3/D18/D19，评审项 10 补测）。

- request.client 缺失时跳过 IP 桶、保留 user 桶（禁止 unknown 全局桶）；
- 可信代理链：直连/可信/不可信/多级 XFF/非法 XFF fail-closed；
- §9.3 bucket 表与 settings 字段/窗口一致性。
"""

from __future__ import annotations

from fastapi import Request

from backend.community.api.dependencies import _RATE_LIMIT_BUCKETS
from backend.shared.client_ip import client_ip


def _request(client: str | None, headers: dict[str, str] | None = None) -> Request:
    scope: dict = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/community/posts",
        "scheme": "http",
        "server": ("testserver", 80),
        "root_path": "",
        "client": (client, 12345) if client else None,
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    return Request(scope)


def test_client_missing_returns_none_for_ip() -> None:
    """request.client 缺失 → 解析返回 None，禁止 unknown 全局桶（§9.3）。"""
    assert client_ip(_request(None), ["10.0.0.0/8"]) is None
    assert client_ip(_request(None), []) is None


def test_client_ip_trusted_proxy_chain() -> None:
    """D18：可信代理从右向左剥离；非法 XFF fail-closed 回退直连。"""
    # 直连可信代理 + 不可信 XFF → 返回第一个不可信地址
    r = _request("10.0.0.1", {"X-Forwarded-For": "203.0.113.7, 10.0.0.2"})
    assert client_ip(r, ["10.0.0.0/8"]) == "203.0.113.7"
    # 整链可信 → 回退直连
    r2 = _request("10.0.0.3", {"X-Forwarded-For": "10.0.0.2, 10.0.0.1"})
    assert client_ip(r2, ["10.0.0.0/8"]) == "10.0.0.3"
    # 非法 XFF 节点 → fail-closed 回退直连
    r3 = _request("10.0.0.1", {"X-Forwarded-For": "not-an-ip, 10.0.0.2"})
    assert client_ip(r3, ["10.0.0.0/8"]) == "10.0.0.1"
    # 无可信代理配置 → 忽略 XFF，返回直连
    r4 = _request("198.51.100.9", {"X-Forwarded-For": "203.0.113.7"})
    assert client_ip(r4, []) == "198.51.100.9"
    # 直连非可信代理 → 忽略 XFF
    r5 = _request("198.51.100.9", {"X-Forwarded-For": "203.0.113.7"})
    assert client_ip(r5, ["10.0.0.0/8"]) == "198.51.100.9"


def test_rate_limit_buckets_table() -> None:
    """§9.3 bucket 表：键与 settings 字段/窗口一致。"""
    assert _RATE_LIMIT_BUCKETS["community.post.create"] == (
        "community_rate_limit_post_per_hour",
        3600,
    )
    assert _RATE_LIMIT_BUCKETS["community.reply.create.minute"] == (
        "community_rate_limit_reply_per_minute",
        60,
    )
    assert _RATE_LIMIT_BUCKETS["community.reply.create.hour"] == (
        "community_rate_limit_reply_per_hour",
        3600,
    )
    assert _RATE_LIMIT_BUCKETS["community.post.like"] == (
        "community_rate_limit_like_per_minute",
        60,
    )
    assert _RATE_LIMIT_BUCKETS["community.read"] == (
        "community_rate_limit_read_per_minute",
        60,
    )
