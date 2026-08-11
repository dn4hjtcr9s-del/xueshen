"""topic_key 规范化单元测试（§4.3 / §23.1）。"""

import pytest

from backend.memory.contracts.common import (
    TopicKeyError,
    normalize_topic_title,
    topic_key_from_title,
    topic_key_with_conflict_suffix,
    validate_existing_topic_key,
)


def test_chinese_title_preserved() -> None:
    assert topic_key_from_title("一致收敛") == "一致收敛"


def test_latin_lowercased() -> None:
    assert topic_key_from_title("Cauchy Sequence") == "cauchy-sequence"


def test_whitespace_and_punctuation_folded() -> None:
    assert topic_key_from_title("极限  的  定义") == "极限-的-定义"
    assert topic_key_from_title("e–d 语言：基础！") == "e-d-语言-基础"


def test_nfkc_normalization() -> None:
    # 全角字符 NFKC 后折叠
    assert topic_key_from_title("Ｌｉｍｉｔ") == "limit"


def test_leading_trailing_and_repeated_dash_removed() -> None:
    key = topic_key_from_title("  --函数--极限--  ")
    assert key == "函数-极限"
    assert not key.startswith("-") and not key.endswith("-")
    assert "--" not in key


def test_max_80_codepoints() -> None:
    key = topic_key_from_title("数" * 200)
    assert len(key) <= 80


def test_empty_after_normalization_rejected() -> None:
    with pytest.raises(TopicKeyError):
        topic_key_from_title("！！！")


def test_control_characters_rejected() -> None:
    with pytest.raises(TopicKeyError):
        topic_key_from_title("极限\u200b定义")  # 零宽空格


def test_path_traversal_rejected() -> None:
    with pytest.raises(TopicKeyError):
        validate_existing_topic_key("../etc")
    with pytest.raises(TopicKeyError):
        validate_existing_topic_key("a/b")
    with pytest.raises(TopicKeyError):
        validate_existing_topic_key("a\\b")
    with pytest.raises(TopicKeyError):
        validate_existing_topic_key("a.b")


def test_conflict_suffix_deterministic() -> None:
    key = topic_key_from_title("极限")
    k1 = topic_key_with_conflict_suffix(key, normalize_topic_title("极限（A）"))
    k2 = topic_key_with_conflict_suffix(key, normalize_topic_title("极限（A）"))
    k3 = topic_key_with_conflict_suffix(key, normalize_topic_title("极限（B）"))
    assert k1 == k2
    assert k1 != k3
    assert len(k1) <= 80
    validate_existing_topic_key(k1)


def test_validate_existing_accepts_normal_key() -> None:
    assert validate_existing_topic_key("cauchy-sequence") == "cauchy-sequence"
    assert validate_existing_topic_key("一致收敛") == "一致收敛"
