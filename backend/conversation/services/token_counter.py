"""TokenCounter：全链路统一 token 计量（附录 A.7）。

- 固定 tiktoken encoding o200k_base（与 RAG chunks 表 tokenizer_id 的
  tiktoken:<encoding> 记录方式一致）；
- history 6000 / memory 3000 / evidence 4000 / answer 2000 / summary 触发 8000
  全部走同一实现；
- 契约写明"token 均指 tiktoken o200k_base 计数"；它是估算而非计费，
  模型网关实际分词差异可接受；
- 测试使用确定性 WhitespaceTokenizer（借鉴 scripts/embedding_chunks/tokenizer.py
  的抽象模式）。
"""

from __future__ import annotations

from typing import Any, Protocol


class Tokenizer(Protocol):
    """tokenizer 抽象（借鉴 scripts/embedding_chunks/tokenizer.py）。"""

    def count(self, text: str) -> int: ...


class TiktokenTokenizer:
    """生产 tokenizer：tiktoken o200k_base（延迟导入，保证测试环境无依赖也可跑）。"""

    _enc = None

    @classmethod
    def _encoding(cls) -> Any:
        if cls._enc is None:
            import tiktoken

            cls._enc = tiktoken.get_encoding("o200k_base")
        return cls._enc

    def count(self, text: str) -> int:
        return len(self._encoding().encode(text))


class WhitespaceTokenizer:
    """测试 tokenizer：确定性（按空白切分），不依赖 tiktoken。"""

    def count(self, text: str) -> int:
        return len(text.split())


class TokenCounter:
    """统一 TokenCounter：所有预算口径一致（附录 A.7）。"""

    def __init__(self, tokenizer: Tokenizer | None = None) -> None:
        self._tokenizer = tokenizer or TiktokenTokenizer()

    def count(self, text: str) -> int:
        return self._tokenizer.count(text)

    def count_messages(self, messages: list[dict[str, object]]) -> int:
        """按消息 content 字段累计 token（§A.7：全链路同一口径）。"""
        total = 0
        for message in messages:
            content = str(message.get("content") or "")
            total += self._tokenizer.count(content)
        return total
