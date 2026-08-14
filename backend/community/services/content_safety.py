"""Community 确定性输入安全校验（方案 §9.4 / §6.2 / D37，v1.6 冻结）。

- 标题/正文去除首尾空白后不能为空；
- 控制字符：禁止 U+0000–U+001F 与 U+007F，白名单仅 \\n \\t \\r（D37）；
- 长度：标题 ≤ 200、正文 ≤ COMMUNITY_*_MAX_LENGTH（Unicode 字符）；
- 最终 SourceItem 组合内容（帖子 = "标题：{title}\\n正文：{body}"，§10.4）
  同时满足单 item ≤ 20,000 字符、UTF-8 ≤ 80,000 bytes（§6.2/§10.4）；
- content_hash = sha256(最终组合内容)，与 Reader 返回给 Memory 的
  SourceItem.content 同一字符串（§7.2 冻结），source_version 依赖此等式。
"""

from __future__ import annotations

import hashlib

from backend.community.contracts.errors import CommunityContentInvalidError

#: SourceItem 契约上限（Memory 侧冻结，§10.4）
_SOURCE_ITEM_MAX_CHARS = 20_000
_SOURCE_BUNDLE_MAX_UTF8_BYTES = 80_000

_TITLE_MAX_CHARS = 200

#: D37：禁止的控制字符集合（白名单 \n \t \r）
_FORBIDDEN_CONTROL = frozenset(chr(c) for c in range(0x20)) - {"\n", "\t", "\r"} | {"\x7f"}


def _reject(message: str) -> CommunityContentInvalidError:
    return CommunityContentInvalidError(message, field="content")


def _validate_no_forbidden_control(value: str) -> None:
    for ch in value:
        if ch in _FORBIDDEN_CONTROL:
            raise _reject("内容包含不允许的控制字符")


def post_content_hash(title: str, body: str) -> str:
    """帖子 content_hash（§7.2 冻结：sha256("标题：{title}\\n正文：{body}")）。"""
    content = f"标题：{title}\n正文：{body}"
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def reply_content_hash(body: str) -> str:
    """回复 content_hash（§7.2 冻结：sha256(body)）。"""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def validate_post(title: str, body: str, *, max_body_chars: int) -> tuple[str, str]:
    """校验并规范化帖子标题/正文；返回 (title, body)（已 strip）。"""
    title = title.strip()
    body = body.strip()
    if not title:
        raise _reject("标题不能为空")
    if not body:
        raise _reject("正文不能为空")
    if len(title) > _TITLE_MAX_CHARS:
        raise _reject(f"标题不能超过 {_TITLE_MAX_CHARS} 个字符")
    if len(body) > max_body_chars:
        raise _reject(f"正文不能超过 {max_body_chars} 个字符")
    _validate_no_forbidden_control(title)
    _validate_no_forbidden_control(body)
    # §6.2/§10.4：组合内容必须满足 SourceItem 契约（写入前校验，Reader 返回前复查）
    content = f"标题：{title}\n正文：{body}"
    _validate_source_item(content)
    return title, body


def validate_reply(body: str, *, max_chars: int) -> str:
    """校验并规范化回复正文；返回 strip 后的 body。"""
    body = body.strip()
    if not body:
        raise _reject("回复不能为空")
    if len(body) > max_chars:
        raise _reject(f"回复不能超过 {max_chars} 个字符")
    _validate_no_forbidden_control(body)
    _validate_source_item(body)
    return body


def _validate_source_item(content: str) -> None:
    """SourceItem 契约：单 item ≤ 20,000 字符、UTF-8 ≤ 80,000 bytes（§10.4）。"""
    if len(content) > _SOURCE_ITEM_MAX_CHARS:
        raise _reject("内容超过允许长度")
    if len(content.encode("utf-8")) > _SOURCE_BUNDLE_MAX_UTF8_BYTES:
        raise _reject("内容体积超过允许范围")
