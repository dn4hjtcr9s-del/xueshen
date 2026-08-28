"""知识总结专用 OpenAI Responses Gateway（知识总结方案 §10、§19.3）。

此 Gateway 与对话回答 Gateway 分离：仅在 generation 开关开启时由 Worker 装配，
且只允许 Responses API 的 Structured Outputs。它不接受工具、检索器或副作用函数，
不会持久化 Prompt、对话正文和原始模型响应。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

from backend.conversation.contracts.knowledge_summary import (
    KnowledgeExtractionResult,
    KnowledgeMergePlanResult,
)
from backend.conversation.knowledge_summary.normalization import KNOWLEDGE_CANONICAL_VERSION
from backend.settings import Settings

EXTRACT_PROMPT_VERSION = "knowledge_extract_v1"
MERGE_PROMPT_VERSION = "knowledge_merge_v1"
EXTRACT_SCHEMA_VERSION = "knowledge_extract_schema_v1"
MERGE_SCHEMA_VERSION = "knowledge_merge_schema_v1"


class KnowledgeSummaryGatewayError(Exception):
    """知识总结模型调用失败的稳定包装，Worker 依据 retryable 分类处理。"""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class KnowledgeSummaryGateway(Protocol):
    """Worker 所依赖的最小模型接口，测试可使用 Fake Gateway 替代。"""

    async def extract(
        self, request: Mapping[str, Any]
    ) -> tuple[KnowledgeExtractionResult, dict[str, int | None]]: ...

    async def merge_plan(
        self, request: Mapping[str, Any]
    ) -> tuple[KnowledgeMergePlanResult, dict[str, int | None]]: ...


class KnowledgeSummaryOpenAIGateway:
    """Responses API Structured Outputs 的生产实现。"""

    def __init__(
        self,
        *,
        settings: Settings,
        logger: logging.Logger | None = None,
        client: Any | None = None,
    ) -> None:
        model = settings.openai_knowledge_summary_model.strip()
        if not settings.openai_api_key:
            raise ValueError("KnowledgeSummaryOpenAIGateway 需要 OPENAI_API_KEY")
        if not model:
            raise ValueError("KnowledgeSummaryOpenAIGateway 需要 OPENAI_KNOWLEDGE_SUMMARY_MODEL")
        if model not in settings.knowledge_summary_structured_output_model_allowlist:
            raise ValueError("知识总结模型不在 Structured Outputs allowlist")
        if settings.conversation_knowledge_summary_sdk_max_retries > 1:
            raise ValueError("知识总结 SDK retry 不得大于 1")

        if client is None:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=settings.openai_api_key,
                base_url=settings.openai_base_url or None,
                timeout=settings.conversation_knowledge_summary_model_timeout_seconds,
                max_retries=settings.conversation_knowledge_summary_sdk_max_retries,
            )
        self._client = client
        self._settings = settings
        self._model = model
        self._logger = logger or logging.getLogger("conversation.gateways.knowledge_summary")
        self._extract_prompt = _load_prompt(EXTRACT_PROMPT_VERSION)
        self._merge_prompt = _load_prompt(MERGE_PROMPT_VERSION)

    @property
    def model_name(self) -> str:
        """返回部署环境显式配置的模型名，供 request hash 和审计使用。"""
        return self._model

    async def extract(
        self, request: Mapping[str, Any]
    ) -> tuple[KnowledgeExtractionResult, dict[str, int | None]]:
        """执行提取 Structured Output，不手写解析 response.output_text。"""
        parsed, usage = await self._parse(
            prompt=self._extract_prompt,
            request=request,
            text_format=KnowledgeExtractionResult,
            max_output_tokens=self._settings.conversation_knowledge_summary_extract_max_output_tokens,
        )
        return parsed, usage

    async def merge_plan(
        self, request: Mapping[str, Any]
    ) -> tuple[KnowledgeMergePlanResult, dict[str, int | None]]:
        """执行合并规划 Structured Output，不手写解析 response.output_text。"""
        parsed, usage = await self._parse(
            prompt=self._merge_prompt,
            request=request,
            text_format=KnowledgeMergePlanResult,
            max_output_tokens=self._settings.conversation_knowledge_summary_merge_max_output_tokens,
        )
        return parsed, usage

    async def _parse(
        self,
        *,
        prompt: str,
        request: Mapping[str, Any],
        text_format: type[Any],
        max_output_tokens: int,
    ) -> tuple[Any, dict[str, int | None]]:
        started = time.monotonic()
        parameters: dict[str, Any] = {
            "model": self._model,
            "instructions": prompt,
            "input": json.dumps(request, ensure_ascii=False, separators=(",", ":"), default=str),
            "text_format": text_format,
            "max_output_tokens": max_output_tokens,
            "timeout": self._settings.conversation_knowledge_summary_model_timeout_seconds,
            "temperature": 0,
        }
        try:
            response = await self._client.responses.parse(**parameters)
        except Exception as exc:
            # 极少数 allowlist 模型不接受 temperature；只针对该参数去除，其他失败不重试。
            if _is_unsupported_temperature_error(exc):
                parameters.pop("temperature")
                try:
                    response = await self._client.responses.parse(**parameters)
                except Exception as retry_exc:
                    raise _map_openai_error(retry_exc) from retry_exc
            else:
                raise _map_openai_error(exc) from exc
        parsed = getattr(response, "output_parsed", None)
        if parsed is None:
            raise KnowledgeSummaryGatewayError("KNOWLEDGE_SUMMARY_SCHEMA_INVALID", retryable=False)
        if not isinstance(parsed, text_format):
            try:
                parsed = text_format.model_validate(parsed)
            except Exception as exc:
                raise KnowledgeSummaryGatewayError(
                    "KNOWLEDGE_SUMMARY_SCHEMA_INVALID", retryable=False
                ) from exc
        usage = getattr(response, "usage", None)
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        return parsed, {
            "input_tokens": _usage_value(usage, "input_tokens"),
            "output_tokens": _usage_value(usage, "output_tokens"),
            "latency_ms": elapsed_ms,
        }


def build_request_hash(
    *,
    model: str,
    purpose: str,
    prompt_version: str,
    schema_version: str,
    input_manifest_hash: str,
    existing_summaries: list[dict[str, Any]],
    request: Mapping[str, Any],
) -> str:
    """按 §19.3 对完整结构化请求做 canonical JSON + SHA-256。"""
    from hashlib import sha256

    payload = {
        "model": model,
        "purpose": purpose,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "normalizer_version": KNOWLEDGE_CANONICAL_VERSION,
        "input_manifest_hash": input_manifest_hash,
        "existing_summaries": sorted(
            existing_summaries, key=lambda item: str(item["summary_id"]).lower()
        ),
        "request": request,
    }
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return sha256(encoded).hexdigest()


def _load_prompt(version: str) -> str:
    """从版本化源码读取冻结 Prompt，运行时不拼接正文。"""
    filename = f"{version}.md"
    return (
        Path(__file__).resolve().parents[1] / "knowledge_summary" / "prompts" / filename
    ).read_text(encoding="utf-8")


def _usage_value(usage: Any, name: str) -> int | None:
    value = getattr(usage, name, None) if usage is not None else None
    return int(value) if isinstance(value, int) else None


def _is_unsupported_temperature_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "temperature" in message and (
        "unsupported" in message or "not allowed" in message or "unknown parameter" in message
    )


def _map_openai_error(exc: Exception) -> KnowledgeSummaryGatewayError:
    """仅暴露稳定错误码，不把供应商异常正文带入 Job 或审计表。"""
    from openai import APIError, APITimeoutError, RateLimitError

    if isinstance(exc, APITimeoutError | TimeoutError):
        return KnowledgeSummaryGatewayError("KNOWLEDGE_SUMMARY_MODEL_TIMEOUT", retryable=True)
    if isinstance(exc, RateLimitError):
        return KnowledgeSummaryGatewayError("KNOWLEDGE_SUMMARY_MODEL_RATE_LIMITED", retryable=True)
    if isinstance(exc, APIError):
        return KnowledgeSummaryGatewayError("KNOWLEDGE_SUMMARY_MODEL_UNAVAILABLE", retryable=True)
    return KnowledgeSummaryGatewayError("KNOWLEDGE_SUMMARY_SCHEMA_INVALID", retryable=False)
