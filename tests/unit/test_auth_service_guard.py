"""测试数据库守卫单元测试（评审 P1-1 / 复审 P3-9）：只允许本链路专用测试库。"""

from __future__ import annotations

import pytest
from _pytest.outcomes import Failed

from tests.integration.conftest import require_test_database

BASE = "postgresql+psycopg://user:pass@127.0.0.1:55432/"


def test_memory_guard_accepts_memory_test() -> None:
    assert require_test_database(BASE + "memory_test", "memory") == "memory_test"
    assert require_test_database(BASE + "memory_test_abc123", "memory") == "memory_test_abc123"


def test_auth_guard_accepts_auth_test() -> None:
    assert require_test_database(BASE + "auth_test", "auth") == "auth_test"


@pytest.mark.parametrize(
    ("url", "prefix"),
    [
        ("memory", "memory"),  # 开发库
        ("auth", "auth"),  # 开发库
        ("auth_test", "memory"),  # 交叉注入：memory 链路连 auth_test
        ("memory_test", "auth"),  # 交叉注入：auth 链路连 memory_test
        ("memory_dev", "memory"),
    ],
)
def test_guard_rejects_non_matching_database(url: str, prefix: str) -> None:
    with pytest.raises(Failed):
        require_test_database(BASE + url, prefix)
