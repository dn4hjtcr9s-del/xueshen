"""OpenAI SDK Gateway（方案 §19）。

- 不写死模型名：按角色从 Settings 读取 OPENAI_REWRITE_MODEL /
  OPENAI_EVIDENCE_MODEL / OPENAI_ANSWER_MODEL / OPENAI_CONVERSATION_SUMMARY_MODEL；
- 使用 Responses API + Structured Outputs（复用 Memory 域的 lenient 解析与错误映射）；
- 对 refusal、内容过滤、空结果和 schema mismatch 分别分类，解析失败只做有限重试
  （由调用方控制 prior_attempts），重试仍失败进入明确降级；
- 不保存隐藏推理，只保存有限枚举 reason_codes 与可观测元数据（§19.3）。
"""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

from backend.conversation.contracts.errors import ModelUnavailableError
from backend.memory.contracts.errors import (
    OpenAISchemaInvalidError,
)
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
        role: Literal["rewrite", "evidence", "answer"],
        system_prompt: str,
        user_payload: str,
        text_format: type[Any],
    ) -> Any:
        from openai.types.shared import ReasoningEffort
        from openai.types.shared_params import Reasoning

        model = self._model_for(role)
        try:
            response = await self._client.responses.create(
                model=model,
                input=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_payload},
                ],
                text={"format": _json_schema_format(text_format)},
                max_output_tokens=ROLE_MAX_OUTPUT[role],
                reasoning=Reasoning(
                    effort=cast(ReasoningEffort, self._settings.openai_reasoning_effort)
                ),
            )
        except Exception as exc:
            raise _map_conversation_openai_error(exc) from exc
        try:
            return _parse_lenient(text_format, response.output_text or "")
        except OpenAISchemaInvalidError as exc:
            self._logger.warning("Structured Output 解析失败: %s", role)
            raise exc

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
        """stream_answer（§19.2）：返回 (deltas, AnswerPayload dict)。"""
        import json as _json

        from backend.conversation.contracts.graph import AnswerPayload
        from backend.conversation.graph.prompts import ANSWER_SYSTEM_PROMPT

        model = self._model_for("answer")
        stream = await self._client.responses.create(
            model=model,
            input=[
                {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _json.dumps(answer_context, ensure_ascii=False, default=str),
                },
            ],
            text={"format": _json_schema_format(AnswerPayload)},
            max_output_tokens=ROLE_MAX_OUTPUT["answer"],
            stream=True,
        )
        deltas: list[str] = []
        try:
            async for event in stream:
                if event.type == "response.output_text.delta":
                    deltas.append(event.delta)
        except Exception as exc:
            raise _map_conversation_openai_error(exc) from exc
        # 评审（第三轮必改 1）：请求带 text.format=json_schema，流出的 delta 是
        # 结构化 JSON 片段；必须把累计文本解析成 AnswerPayload 再返回，
        # 否则持久化的 answer 是原始 JSON、followups 恒空。
        raw = "".join(deltas)
        try:
            payload = _parse_lenient(AnswerPayload, raw)
        except OpenAISchemaInvalidError as exc:
            # 流式 JSON 不完整/解析失败：按 schema 失败处理（与 _structured 同分类）
            raise exc
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
