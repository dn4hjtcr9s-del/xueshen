"""验证 tokenizer protocol 的确定性计数和可逆切片。"""

from scripts.embedding_chunks.tokenizer import WhitespaceTokenizer


def test_whitespace_tokenizer_counts_and_decodes_tokens() -> None:
    tokenizer = WhitespaceTokenizer()

    tokens = tokenizer.encode("alpha beta gamma")

    assert tokenizer.tokenizer_id == "whitespace-v1"
    assert tokenizer.count("alpha beta gamma") == 3
    assert tokenizer.decode(tokens[1:]) == "beta gamma"
