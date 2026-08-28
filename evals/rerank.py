"""评测运行器使用的 OpenAI 兼容 Rerank 客户端。

从环境变量读取 RERANK_BASE_URL、RERANK_MODEL，并优先使用 RERANK_API_KEY；
未配置专用密钥时复用 DASHSCOPE_API_KEY。模块不会记录或输出任何密钥。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

MATHEMATICS_RERANK_INSTRUCT = (
    "Given a mathematics textbook question, retrieve passages that directly and completely "
    "answer the question."
)

# 评测确认 qwen3-rerank 对额外结构化包装敏感，因此保留用户原问题和教材原文。
RERANK_QUERY_STRATEGY = "raw-query/no-rewrite/v3"
RERANK_DOCUMENT_STRATEGY = "raw-content-text/no-metadata-prefix/v3"


class RerankRequestError(RuntimeError):
    """Rerank 服务请求或返回结构不符合预期。"""


@dataclass(frozen=True, slots=True)
class RerankSettings:
    """一次 Rerank 调用的脱敏配置；密钥不参与 repr。"""

    base_url: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 60.0
    instruct: str = MATHEMATICS_RERANK_INSTRUCT

    @classmethod
    def from_sources(
        cls,
        *,
        env_file: Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> RerankSettings:
        """按进程环境覆盖 .env 的顺序读取 Rerank 配置。"""
        values = {
            **_read_env_file(env_file),
            **dict(os.environ if environ is None else environ),
        }
        base_url = values.get("RERANK_BASE_URL", "").strip()
        if not base_url:
            raise RerankRequestError("缺少 RERANK_BASE_URL")
        model = values.get("RERANK_MODEL", "").strip()
        if not model:
            raise RerankRequestError("缺少 RERANK_MODEL")
        api_key = (
            values.get("RERANK_API_KEY", "").strip() or values.get("DASHSCOPE_API_KEY", "").strip()
        )
        if not api_key:
            raise RerankRequestError("缺少 Rerank API key（RERANK_API_KEY 或 DASHSCOPE_API_KEY）")
        try:
            timeout_seconds = float(values.get("RERANK_TIMEOUT_SECONDS", "60"))
        except ValueError as exc:
            raise RerankRequestError("RERANK_TIMEOUT_SECONDS 必须为正数") from exc
        if timeout_seconds <= 0:
            raise RerankRequestError("RERANK_TIMEOUT_SECONDS 必须为正数")
        return cls(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
        )


def _read_env_file(path: Path | None) -> dict[str, str]:
    """读取简单 .env 文件，仅供评测时加载非机密配置。"""
    if path is None or not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        value = raw_value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


@dataclass(frozen=True, slots=True)
class RerankResult:
    """Rerank 服务返回的候选索引与相关性分数。"""

    index: int
    relevance_score: float


class RerankClient:
    """调用 OpenAI 兼容 ``/reranks`` 接口的同步客户端。"""

    def __init__(self, settings: RerankSettings, *, client: httpx.Client | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.Client(
            timeout=settings.timeout_seconds,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
        )
        self._owns_client = client is None

    def close(self) -> None:
        """关闭模块自行创建的 HTTP 连接池。"""
        if self._owns_client:
            self._client.close()

    def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        top_n: int,
    ) -> tuple[RerankResult, ...]:
        """重排候选正文，并返回按服务相关性降序排列的原始候选索引。"""
        if not query.strip():
            raise RerankRequestError("Rerank query 不能为空")
        if not documents:
            raise RerankRequestError("Rerank documents 不能为空")
        if top_n < 1 or top_n > len(documents):
            raise RerankRequestError("Rerank top_n 必须在 1 到候选数之间")
        payload = {
            "model": self._settings.model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
            "return_documents": False,
            "instruct": self._settings.instruct,
        }
        try:
            response = self._client.post(
                self._settings.base_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self._settings.api_key}",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RerankRequestError("Rerank 请求失败") from exc
        return _parse_rerank_results(data, document_count=len(documents), top_n=top_n)


def _parse_rerank_results(
    payload: Any,
    *,
    document_count: int,
    top_n: int,
) -> tuple[RerankResult, ...]:
    """兼容顶层或 ``output`` 包裹的 OpenAI 风格结果，并校验索引完整性。"""
    if not isinstance(payload, dict):
        raise RerankRequestError("Rerank 返回必须是 JSON 对象")
    raw_results = payload.get("results")
    if raw_results is None and isinstance(payload.get("output"), dict):
        raw_results = payload["output"].get("results")
    if not isinstance(raw_results, list) or len(raw_results) != top_n:
        raise RerankRequestError("Rerank 返回结果数量异常")

    parsed: list[RerankResult] = []
    indexes: set[int] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            raise RerankRequestError("Rerank 返回结果项必须是对象")
        index = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < document_count:
            raise RerankRequestError("Rerank 返回了非法候选索引")
        if index in indexes:
            raise RerankRequestError("Rerank 返回了重复候选索引")
        if isinstance(score, bool) or not isinstance(score, int | float):
            raise RerankRequestError("Rerank 返回了非法相关性分数")
        indexes.add(index)
        parsed.append(RerankResult(index=index, relevance_score=float(score)))
    return tuple(parsed)
