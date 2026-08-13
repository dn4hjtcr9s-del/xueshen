"""认证服务账号/密码规则单元测试（方案 §4.3 / §11）。"""

from __future__ import annotations

import pytest

from backend.auth_service.errors import AuthServiceError
from backend.auth_service.security import (
    hash_password,
    new_refresh_token,
    normalize_email,
    normalize_username,
    refresh_token_hash,
    validate_email,
    validate_password,
    validate_username,
    verify_password,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("alice", "alice"),
        ("  alice  ", "alice"),
        ("ALICE_01", "alice_01"),
        ("a1", None),  # 过短
        ("a" * 65, None),  # 过长
        ("_alice", None),  # 下划线开头
        ("-alice", None),  # 非法字符
        ("alice-01", None),
        ("爱丽丝", None),
        ("alice@x", None),
    ],
)
def test_username_normalize_and_validate(raw: str, expected: str | None) -> None:
    if expected is None:
        with pytest.raises(AuthServiceError) as excinfo:
            validate_username(raw)
        assert excinfo.value.code == "AUTH_INVALID_USERNAME"
    else:
        assert normalize_username(raw) == expected
        assert validate_username(raw) == expected


@pytest.mark.parametrize(
    "reserved",
    ["admin", "administrator", "root", "system", "support", "official", "gewu", "api", "www"],
)
def test_reserved_usernames_rejected(reserved: str) -> None:
    with pytest.raises(AuthServiceError) as excinfo:
        validate_username(reserved)
    assert excinfo.value.code == "AUTH_INVALID_USERNAME"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Alice@Example.com", "alice@example.com"),
        ("  a@b.co  ", "a@b.co"),
        ("not-an-email", None),
        ("a@b", None),  # 无点
        ("a b@c.de", None),
        ("@" + "a" * 400 + ".com", None),  # 超长
    ],
)
def test_email_normalize_and_validate(raw: str, expected: str | None) -> None:
    if expected is None:
        with pytest.raises(AuthServiceError) as excinfo:
            validate_email(raw)
        assert excinfo.value.code == "AUTH_INVALID_EMAIL"
    else:
        assert normalize_email(raw) == expected
        assert validate_email(raw) == expected


@pytest.mark.parametrize(
    "password",
    [
        "abc1234567",
        "a1" + "x" * 70,
        "a1!@#$%^&*()",
    ],
)
def test_valid_passwords(password: str) -> None:
    validate_password(password)  # 不抛异常即通过


@pytest.mark.parametrize(
    "password",
    [
        "a1",  # 过短
        "abcdefghijk",  # 无数字
        "1234567890",  # 无字母
        "密码密码密码密码密码1",  # 非 ASCII 字母
        "a1" + "x" * 71,  # UTF-8 超 72 字节
        "a1" + "中" * 30,  # 多字节字符超限
    ],
)
def test_invalid_passwords(password: str) -> None:
    with pytest.raises(AuthServiceError) as excinfo:
        validate_password(password)
    assert excinfo.value.code == "AUTH_WEAK_PASSWORD"


def test_password_hash_verify_roundtrip() -> None:
    hashed = hash_password("correct-horse-123")
    assert hashed != "correct-horse-123"
    assert verify_password("correct-horse-123", hashed)
    assert not verify_password("wrong-password-123", hashed)


def test_verify_password_malformed_hash_is_false() -> None:
    assert not verify_password("whatever-123", "not-a-bcrypt-hash")


def test_refresh_token_entropy_and_hash() -> None:
    a = new_refresh_token()
    b = new_refresh_token()
    assert a != b
    digest = refresh_token_hash(a)
    assert isinstance(digest, bytes)
    assert len(digest) == 32  # SHA-256
    assert refresh_token_hash(a) == digest
    assert refresh_token_hash(a) != refresh_token_hash(b)
