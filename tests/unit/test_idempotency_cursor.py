"""canonical JSON、幂等 hash 与 cursor 签名单元测试（§4.5 / §19.9）。"""

import time

import pytest

from backend.memory.contracts.common import (
    CursorError,
    canonical_json,
    cursor_principal_hash,
    hmac_hex,
    idempotency_payload_hash,
    sign_cursor,
    verify_cursor,
)

KEY = "test-cursor-key"


def test_canonical_json_key_sorted() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'


def test_canonical_json_no_whitespace() -> None:
    assert canonical_json({"a": [1, 2], "b": {"c": True, "d": None}}) == (
        '{"a":[1,2],"b":{"c":true,"d":null}}'
    )


def test_canonical_json_string_escapes() -> None:
    assert canonical_json('a"b\nc') == '"a\\"b\\nc"'
    assert canonical_json("一致收敛") == '"一致收敛"'  # 非 ASCII 不转义


def test_canonical_json_numbers() -> None:
    assert canonical_json(3) == "3"
    assert canonical_json(0.66) == "0.66"
    assert canonical_json(1e21) == "1e21"


def test_idempotency_hash_stable_and_order_independent() -> None:
    h1 = idempotency_payload_hash({"a": 1, "b": [2, 3]})
    h2 = idempotency_payload_hash({"b": [2, 3], "a": 1})
    h3 = idempotency_payload_hash({"a": 1, "b": [2, 4]})
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64 and h1 == h1.lower()


def _sign(filters: dict | None = None, ttl: float = 900) -> str:
    payload = {
        "cursor_version": 1,
        "route": "memory.search",
        "principal_hash": cursor_principal_hash(KEY, "user-1"),
        "normalized_filters": filters or {"query": "极限"},
        "sort_key": ["2026-08-10T08:00:00Z", "mastery:x"],
        "expires_at": time.time() + ttl,
    }
    return sign_cursor(KEY, payload)


def test_cursor_roundtrip() -> None:
    token = _sign()
    payload = verify_cursor(
        KEY,
        token,
        route="memory.search",
        principal_hash=cursor_principal_hash(KEY, "user-1"),
        normalized_filters={"query": "极限"},
        now_epoch=time.time(),
    )
    assert payload["route"] == "memory.search"


def test_cursor_tampered_rejected() -> None:
    token = _sign()
    body, _sig = token.rsplit(".", 1)
    forged = body + "." + "0" * 64
    with pytest.raises(CursorError) as exc:
        verify_cursor(
            KEY,
            forged,
            route="memory.search",
            principal_hash=cursor_principal_hash(KEY, "user-1"),
            normalized_filters={"query": "极限"},
            now_epoch=time.time(),
        )
    assert exc.value.code == "CURSOR_INVALID"


def test_cursor_route_mismatch() -> None:
    token = _sign()
    with pytest.raises(CursorError) as exc:
        verify_cursor(
            KEY,
            token,
            route="memory.deleted",
            principal_hash=cursor_principal_hash(KEY, "user-1"),
            normalized_filters={"query": "极限"},
            now_epoch=time.time(),
        )
    assert exc.value.code == "CURSOR_INVALID"


def test_cursor_principal_mismatch() -> None:
    token = _sign()
    with pytest.raises(CursorError) as exc:
        verify_cursor(
            KEY,
            token,
            route="memory.search",
            principal_hash=cursor_principal_hash(KEY, "user-2"),
            normalized_filters={"query": "极限"},
            now_epoch=time.time(),
        )
    assert exc.value.code == "CURSOR_INVALID"


def test_cursor_expired() -> None:
    token = _sign(ttl=-10)
    with pytest.raises(CursorError) as exc:
        verify_cursor(
            KEY,
            token,
            route="memory.search",
            principal_hash=cursor_principal_hash(KEY, "user-1"),
            normalized_filters={"query": "极限"},
            now_epoch=time.time(),
        )
    assert exc.value.code == "CURSOR_EXPIRED"


def test_hmac_domain_separation() -> None:
    assert hmac_hex(KEY, "user:v1", "u") != hmac_hex(KEY, "privacy-audit:v1", "u")
