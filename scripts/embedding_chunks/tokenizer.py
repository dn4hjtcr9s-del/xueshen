"""Tokenizer 抽象；生产使用 tiktoken，测试使用确定性空白 tokenizer。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, cast

type Token = int | str


class Tokenizer(Protocol):
    """切块器所需的最小可逆 tokenizer 接口。"""

    @property
    def tokenizer_id(self) -> str: ...

    def encode(self, text: str) -> list[Token]: ...

    def decode(self, tokens: Sequence[Token]) -> str: ...

    def count(self, text: str) -> int: ...


class WhitespaceTokenizer:
    """仅用于单元测试的确定性 tokenizer，不用于正式产物。"""

    tokenizer_id = "whitespace-v1"

    def encode(self, text: str) -> list[Token]:
        return cast(list[Token], text.split())

    def decode(self, tokens: Sequence[Token]) -> str:
        return " ".join(str(token) for token in tokens)

    def count(self, text: str) -> int:
        return len(self.encode(text))


class TiktokenTokenizer:
    """基于指定 tiktoken encoding 的生产 tokenizer。"""

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        try:
            import tiktoken
        except ImportError as exc:
            raise RuntimeError("缺少 tiktoken；请安装项目的 embedding optional dependency") from exc
        self._encoding = tiktoken.get_encoding(encoding_name)
        self._tokenizer_id = f"tiktoken:{encoding_name}"

    @property
    def tokenizer_id(self) -> str:
        return self._tokenizer_id

    def encode(self, text: str) -> list[Token]:
        return list(self._encoding.encode(text))

    def decode(self, tokens: Sequence[Token]) -> str:
        token_ids = [cast(int, token) for token in tokens]
        return str(self._encoding.decode(token_ids))

    def count(self, text: str) -> int:
        return len(self._encoding.encode(text))
