"""认证服务限流与客户端地址解析单元测试（方案 §10.1 / 附录 A.2 #9）。"""

from __future__ import annotations

from fastapi import Request

from backend.auth_service import ratelimit as rl
from backend.auth_service.mapping_consumer import backoff_seconds


def _request_with_headers(headers: dict[str, str], client_host: str) -> Request:
    scope = {
        "type": "http",
        "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        "client": (client_host, 12345),
        "server": ("testserver", 80),
        "method": "POST",
        "path": "/api/v1/auth/login",
        "scheme": "http",
    }
    return Request(scope=scope)


def test_client_ip_direct_only_by_default() -> None:
    req = _request_with_headers(
        {"x-forwarded-for": "1.2.3.4"}, "10.0.0.5"
    )
    assert rl.client_ip(req, []) == "10.0.0.5"


def test_client_ip_trusts_proxy_only_for_whitelisted_peer() -> None:
    req = _request_with_headers({"x-forwarded-for": "1.2.3.4"}, "10.0.0.5")
    assert rl.client_ip(req, ["10.0.0.0/8"]) == "1.2.3.4"
    # 直连对端不在白名单：忽略转发头
    assert rl.client_ip(req, ["192.168.0.0/16"]) == "10.0.0.5"


def test_client_ip_invalid_xff_falls_back_to_direct() -> None:
    req = _request_with_headers({"x-forwarded-for": "not-an-ip"}, "10.0.0.5")
    assert rl.client_ip(req, ["10.0.0.0/8"]) == "10.0.0.5"


def test_client_ip_xff_first_entry_used() -> None:
    req = _request_with_headers(
        {"x-forwarded-for": "1.2.3.4, 5.6.7.8"}, "10.0.0.5"
    )
    assert rl.client_ip(req, ["10.0.0.0/8"]) == "1.2.3.4"


def test_retry_after_is_positive_and_bounded() -> None:
    assert 1 <= rl.retry_after_seconds() <= 60


def test_backoff_grows_to_cap() -> None:
    assert backoff_seconds(1) == 30.0
    assert backoff_seconds(2) == 60.0
    assert backoff_seconds(3) == 120.0
    assert backoff_seconds(10) == 3600.0
    assert backoff_seconds(20) == 3600.0
