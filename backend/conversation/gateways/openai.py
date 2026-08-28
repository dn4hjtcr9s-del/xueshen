"""OpenAI SDK Gateway（方案 §19）。

- 不写死模型名：按角色从 Settings 读取 OPENAI_REWRITE_MODEL /
  OPENAI_EVIDENCE_MODEL / OPENAI_ANSWER_MODEL / OPENAI_CONVERSATION_SUMMARY_MODEL；
- Responses Structured Outputs 会显式识别 incomplete、failed、refusal、空正文和
  schema mismatch，并在网关内有限重试；
- Rewrite/Evidence 的结构化输出使用 reasoning=none，失败后由 Graph 节点确定性降级；
- Answer 先非流式取得并校验完整结构，再由应用层切分正文 delta，避免流式 JSON 截断；
- 日志不记录完整 Prompt、模型正文或凭证，只记录角色、次数和失败分类（§19.3）。
"""

from __future__ import annotations

import logging
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel

from backend.conversation.contracts.errors import (
    ModelUnavailableError,
    StructuredOutputError,
)
from backend.memory.contracts.errors import OpenAISchemaInvalidError
from backend.memory.graph.openai_client import _json_schema_format, _parse_lenient
from backend.settings import Settings

ROLE_MODELS = {
    "rewrite": "openai_rewrite_model",
    "evidence": "openai_evidence_model",
    "answer": "openai_answer_model",
    "summary": "openai_conversation_summary_model",
}

ROLE_TIMEOUTS: dict[str, float] = {
    "rewrite": 30.0,
    "evidence": 30.0,
    "answer": 120.0,
    "summary": 60.0,
}

ROLE_MAX_OUTPUT: dict[str, int] = {
    "rewrite": 1500,
    "evidence": 1000,
    "answer": 3000,
    "summary": 1200,
}

STRUCTURED_MAX_ATTEMPTS = 3
ANSWER_DELTA_CHARS = 64
_RETRY_OUTPUT_CONSTRAINT = (
    "这是结构化输出重试。只返回符合 JSON Schema 的单个 JSON 对象，"
    "不要输出 Markdown 代码围栏、解释、前后缀或额外字段。"
)

TStructured = TypeVar("TStructured", bound=BaseModel)
StructuredRole = Literal["rewrite", "evidence", "answer"]


class OpenAIGateway:
    """Conversation 域 OpenAI Gateway（Real 实现）。"""

    def __init__(
        self,
        *,
        settings: Settings,
        logger: logging.Logger | None = None,
    ) -> None:
        if not settings.openai_api_key:
            raise ValueError("OpenAIGateway 需要 OPENAI_API_KEY")
        from openai import AsyncOpenAI

        self._settings = settings
        self._logger = logger or logging.getLogger("conversation.gateways.openai")
        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
            timeout=max(ROLE_TIMEOUTS.values()),
        )

    def _model_for(self, role: str) -> str:
        model = getattr(self._settings, ROLE_MODELS[role], "")
        if not model:
            raise ModelUnavailableError(f"未配置 {ROLE_MODELS[role]} 环境变量")
        return model

    async def _structured(
        self,
        *,
        role: StructuredRole,
        system_prompt: str,
        user_payload: str,
        text_format: type[TStructured],
    ) -> TStructured:
        """完整取得并校验结构化输出；失败最多调用模型三次。"""
        from openai.types.shared import ReasoningEffort
        from openai.types.shared_params import Reasoning

        model = self._model_for(role)
        reasoning_effort = (
            "none" if role in {"rewrite", "evidence"} else self._settings.openai_reasoning_effort
        )
        last_error: ModelUnavailableError | None = None

        for attempt in range(1, STRUCTURED_MAX_ATTEMPTS + 1):
            attempt_prompt = system_prompt
            if attempt > 1:
                attempt_prompt = f"{system_prompt}\n\n{_RETRY_OUTPUT_CONSTRAINT}"
            try:
                response = await self._client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": attempt_prompt},
                        {"role": "user", "content": user_payload},
                    ],
                    text={"format": _json_schema_format(text_format)},
                    max_output_tokens=ROLE_MAX_OUTPUT[role],
                    reasoning=Reasoning(effort=cast(ReasoningEffort, reasoning_effort)),
                    timeout=ROLE_TIMEOUTS[role],
                )
            except Exception as exc:
                mapped = _map_conversation_openai_error(exc)
                if not isinstance(mapped, ModelUnavailableError):
                    if mapped is exc:
                        raise
                    raise mapped from exc
                last_error = mapped
                self._log_structured_failure(role=role, attempt=attempt, error=mapped)
                if attempt >= STRUCTURED_MAX_ATTEMPTS:
                    raise mapped from exc
                continue

            try:
                return _parse_structured_response(
                    response,
                    text_format=text_format,
                    attempt=attempt,
                )
            except StructuredOutputError as exc:
                last_error = exc
                self._log_structured_failure(role=role, attempt=attempt, error=exc)
                if attempt >= STRUCTURED_MAX_ATTEMPTS:
                    raise

        if last_error is not None:  # pragma: no cover - 循环穷尽保护
            raise last_error
        raise ModelUnavailableError("结构化输出调用未执行")  # pragma: no cover

    def _log_structured_failure(
        self,
        *,
        role: StructuredRole,
        attempt: int,
        error: ModelUnavailableError,
    ) -> None:
        """只记录结构化调用诊断元数据，不记录 Prompt 或模型正文。"""
        self._logger.warning(
            "Structured Output 失败 role=%s attempt=%d/%d reason=%s status=%s incomplete=%s",
            role,
            attempt,
            STRUCTURED_MAX_ATTEMPTS,
            getattr(error, "reason", "model_unavailable"),
            getattr(error, "response_status", None),
            getattr(error, "incomplete_reason", None),
        )

    async def rewrite_and_plan(
        self, *, context_view: dict[str, Any], prior_attempts: int
    ) -> dict[str, Any]:
        """rewrite_and_plan（§19.2）：输出 RewritePlan dict。"""
        import json as _json

        from backend.conversation.contracts.graph import RewritePlan
        from backend.conversation.graph.prompts import REWRITE_SYSTEM_PROMPT

        user_payload = _json.dumps(context_view, ensure_ascii=False, default=str)
        plan = await self._structured(
            role="rewrite",
            system_prompt=REWRITE_SYSTEM_PROMPT,
            user_payload=user_payload,
            text_format=RewritePlan,
        )
        return dict(plan.model_dump(mode="json"))

    async def assess_evidence(
        self, *, question: str, evidence_summary: str, budget_remaining: str
    ) -> dict[str, Any]:
        """assess_evidence（§19.2）：输出 EvidenceAssessment dict。"""
        import json as _json

        from backend.conversation.contracts.graph import EvidenceAssessment
        from backend.conversation.graph.prompts import EVIDENCE_SYSTEM_PROMPT

        payload = {
            "question": question,
            "evidence_summary": evidence_summary,
            "budget_remaining": budget_remaining,
        }
        assessment = await self._structured(
            role="evidence",
            system_prompt=EVIDENCE_SYSTEM_PROMPT,
            user_payload=_json.dumps(payload, ensure_ascii=False),
            text_format=EvidenceAssessment,
        )
        return dict(assessment.model_dump(mode="json"))

    async def stream_answer(
        self, *, answer_context: dict[str, Any]
    ) -> tuple[list[str], dict[str, Any]]:
        """完整校验 Answer 后切分正文，兼容现有应用层 delta 协议。"""
        import json as _json

        from backend.conversation.contracts.graph import AnswerGenerationOutput
        from backend.conversation.graph.prompts import ANSWER_SYSTEM_PROMPT

        payload = await self._structured(
            role="answer",
            system_prompt=ANSWER_SYSTEM_PROMPT,
            user_payload=_json.dumps(answer_context, ensure_ascii=False, default=str),
            text_format=AnswerGenerationOutput,
        )
        answer = payload.answer
        deltas = [
            answer[index : index + ANSWER_DELTA_CHARS]
            for index in range(0, len(answer), ANSWER_DELTA_CHARS)
        ]
        return deltas, payload.model_dump(mode="json")

    async def summarize_conversation(
        self, *, messages: list[dict[str, Any]], previous_summary: str | None
    ) -> str:
        """summarize_conversation（§19.2）：输出摘要正文。"""
        import json as _json

        from backend.conversation.graph.prompts import SUMMARY_SYSTEM_PROMPT

        model = self._model_for("summary")
        payload = {"messages": messages, "previous_summary": previous_summary}
        response = await self._client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _json.dumps(payload, ensure_ascii=False, default=str),
                },
            ],
            max_output_tokens=ROLE_MAX_OUTPUT["summary"],
        )
        text = (response.output_text or "").strip()
        if not text:
            raise ModelUnavailableError("摘要模型返回空内容")
        return text


def _parse_structured_response[TResponse: BaseModel](
    response: Any,
    *,
    text_format: type[TResponse],
    attempt: int,
) -> TResponse:
    """识别 Responses 状态并严格校验完整 JSON，不修补截断内容。"""
    status = _string_field(response, "status")
    incomplete_reason = _incomplete_reason(response)
    if status == "incomplete":
        raise StructuredOutputError(
            "模型结构化输出未完成",
            reason="incomplete",
            attempts=attempt,
            response_status=status,
            incomplete_reason=incomplete_reason,
        )
    if status == "failed":
        raise StructuredOutputError(
            "模型结构化输出失败",
            reason="failed",
            attempts=attempt,
            response_status=status,
            incomplete_reason=incomplete_reason,
        )
    if status and status != "completed":
        raise StructuredOutputError(
            "模型返回非终态结构化响应",
            reason="unexpected_status",
            attempts=attempt,
            response_status=status,
            incomplete_reason=incomplete_reason,
        )

    text, refusal = _extract_response_text(response)
    if refusal is not None:
        raise StructuredOutputError(
            "模型拒绝生成结构化输出",
            reason="refusal",
            attempts=attempt,
            response_status=status or None,
            incomplete_reason=incomplete_reason,
        )
    if not text:
        raise StructuredOutputError(
            "模型返回空结构化输出",
            reason="empty_output",
            attempts=attempt,
            response_status=status or None,
            incomplete_reason=incomplete_reason,
        )
    try:
        return _parse_lenient(text_format, text)
    except OpenAISchemaInvalidError as exc:
        raise StructuredOutputError(
            "模型结构化输出不符合 Schema",
            reason="schema_invalid",
            attempts=attempt,
            response_status=status or None,
            incomplete_reason=incomplete_reason,
        ) from exc


def _extract_response_text(response: Any) -> tuple[str, str | None]:
    """兼容官方 SDK 与兼容端点：output_text 为空时回退扫描 output。"""
    direct = _field(response, "output_text")
    direct_text = direct.strip() if isinstance(direct, str) else ""
    texts: list[str] = []
    refusal = _content_text(_field(response, "refusal")) or None

    for item in _items(_field(response, "output")):
        item_type = _string_field(item, "type")
        if item_type == "refusal":
            refusal = _content_text(_field(item, "refusal")) or "refusal"
            continue
        item_text = _content_text(_field(item, "text"))
        if item_type in {"output_text", "text"} and item_text:
            texts.append(item_text)
        for content in _items(_field(item, "content")):
            content_type = _string_field(content, "type")
            refusal_text = _content_text(_field(content, "refusal"))
            if content_type == "refusal" or refusal_text:
                refusal = refusal_text or "refusal"
                continue
            text = _content_text(_field(content, "text"))
            if content_type in {"output_text", "text", ""} and text:
                texts.append(text)

    if refusal is not None:
        return "", refusal
    if direct_text:
        return direct_text, None
    return "".join(texts).strip(), None


def _incomplete_reason(response: Any) -> str | None:
    details = _field(response, "incomplete_details")
    reason = _field(details, "reason")
    return str(reason) if reason not in {None, ""} else None


def _field(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _string_field(value: Any, name: str) -> str:
    field = _field(value, name)
    return str(field) if field not in {None, ""} else ""


def _items(value: Any) -> list[Any]:
    if isinstance(value, list | tuple):
        return list(value)
    return []


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    nested = _field(value, "value")
    if isinstance(nested, str):
        return nested
    return ""


def _map_conversation_openai_error(exc: Exception) -> Exception:
    """SDK 异常 → Conversation 域错误（§19.3 分类）。"""
    from openai import APIError, APITimeoutError, RateLimitError

    if isinstance(exc, APITimeoutError | TimeoutError):
        return ModelUnavailableError(f"模型调用超时: {str(exc)[:200]}")
    if isinstance(exc, RateLimitError):
        return ModelUnavailableError(f"模型限流: {str(exc)[:200]}")
    if isinstance(exc, APIError):
        return ModelUnavailableError(f"模型调用失败: {str(exc)[:200]}")
    return exc
