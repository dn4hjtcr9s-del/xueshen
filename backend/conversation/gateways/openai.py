"""OpenAI SDK Gateway（方案 §19）。

- 不写死模型名：按角色从 Settings 读取 OPENAI_REWRITE_MODEL /
  OPENAI_EVIDENCE_MODEL / OPENAI_ANSWER_MODEL / OPENAI_CONVERSATION_SUMMARY_MODEL；
- Responses Structured Outputs 会显式识别 incomplete、failed、refusal、空正文和
  schema mismatch，并在网关内有限重试；
- Rewrite/Evidence 的结构化输出使用 reasoning=none，失败后由 Graph 节点确定性降级；
- Answer 先非流式取得并校验完整结构，再由应用层切分正文 delta，避免流式 JSON 截断；
- Answer 可参与记忆工具循环（memory-rebuild §2.4 D3③ / §5.7）：显式传入 ``tools``
  时把 ``memory.search`` / ``memory.read`` 函数定义下发给模型，模型请求工具就结束
  本轮并把调用结构交给调用方（图）执行，再由调用方用 ``previous_response_id`` +
  ``tool_outputs`` 续接下一轮；网关**不执行任何工具**。工具轮里模型一并给出的
  preamble 正文（"我先查一下你的学习记录"）在流式/非流式两种模式下都**保留**，
  由调用方按轮次顺序拼接成最终正文，保证"已发出的 delta 一定是最终正文前缀"；
- 日志不记录完整 Prompt、模型正文或凭证，只记录角色、次数和失败分类（§19.3）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, TypeVar, cast, overload

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
    "rewrite": 2500,
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


# ---------------------------------------------------------------------------
# 记忆工具（memory-rebuild §2.4 D3③ / §5.7 Phase 5）
# ---------------------------------------------------------------------------

#: 下发给 Responses API 的线上函数名。函数名只允许 ``[A-Za-z0-9_-]``（最长 64 字符），
#: **点号会被服务端拒绝**，因此线上用下划线形式；网关在解析 ``function_call`` 时再
#: 映射回 §2.4 冻结的规范工具名（与 ``contracts/rollout.py`` 的 ``MemoryToolName`` 一致）。
MEMORY_SEARCH_WIRE_NAME = "memory_search"
MEMORY_READ_WIRE_NAME = "memory_read"

#: 线上函数名 → 规范工具名；未知名字原样透传，由调用方判定能否执行。
MEMORY_TOOL_CANONICAL_NAMES: dict[str, str] = {
    MEMORY_SEARCH_WIRE_NAME: "memory.search",
    MEMORY_READ_WIRE_NAME: "memory.read",
}

#: 参数不是合法 JSON 对象时，``arguments_raw`` 保留的原始字符数上限（仅诊断用，
#: 不进日志、不进正文）。
TOOL_ARGUMENTS_RAW_MAX_CHARS = 500


def memory_tool_specs() -> list[dict[str, Any]]:
    """``memory.search`` / ``memory.read`` 的 Responses API ``tools`` 定义（§2.4 D3）。

    返回**新副本**，调用方可直接作为 ``tools=`` 传给
    :meth:`OpenAIGateway.stream_answer` 或 :meth:`OpenAIGateway.open_answer_stream`。
    JSON Schema 与 ``backend/memory/contracts/results.py`` 的
    ``MemoryToolSearchRequest`` / ``MemoryToolReadRequest`` 约束一致：选项参数不进
    ``required``，并用 ``strict=false``（strict 模式要求全部字段必填且不接受 null，
    与"省略即取默认值"的 Pydantic 契约冲突）。
    """
    return [
        {
            "type": "function",
            "name": MEMORY_SEARCH_WIRE_NAME,
            "description": (
                "在当前用户的长期记忆注册表里做纯关键词定位，返回条目元数据"
                "（memory_id / name / description / keywords / version / updated_at），"
                "不含正文。用于回答涉及用户学习历史、已掌握知识点或稳定偏好的问题："
                "先用本工具定位条目，再用 memory_read 读取正文。"
            ),
            "strict": False,
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "description": "关键词列表（1-8 条，每条 1-200 字符），用词而非整句。",
                        "items": {"type": "string", "minLength": 1, "maxLength": 200},
                        "minItems": 1,
                        "maxItems": 8,
                    },
                    "match_mode": {
                        "type": "string",
                        "enum": ["any", "all"],
                        "description": (
                            "any = 命中任一关键词即入选（默认）；all = 须命中全部关键词。"
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "返回条目数上限，默认 10。",
                    },
                },
                "required": ["queries"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": MEMORY_READ_WIRE_NAME,
            "description": (
                "按行分段读取当前用户某条长期记忆的正文，返回正文片段、行范围、"
                "version 与 checksum（供回答引用回查）。只能读 memory_search 返回的 "
                "memory_id；正文分段返回，后续片段用 line_offset 续读。"
            ),
            "strict": False,
            "parameters": {
                "type": "object",
                "properties": {
                    "memory_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 160,
                        "description": "memory_search 返回的 memory_id，不要猜测或编造。",
                    },
                    "line_offset": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "起始行号（0 起，默认 0）。",
                    },
                    "max_lines": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 500,
                        "description": "最多返回行数，默认 200。",
                    },
                },
                "required": ["memory_id"],
                "additionalProperties": False,
            },
        },
    ]


@dataclass(frozen=True)
class AnswerToolTurn:
    """非流式工具轮结果（``stream_answer(tools=...)`` 的返回形态）。

    - ``pending_tool_calls`` 非空：本轮模型请求了工具，调用方执行后用
      ``previous_response_id`` + ``tool_outputs`` 续接下一轮；此时 ``deltas`` /
      ``payload`` 是该轮一并生成的 preamble 正文（没有正文时为空列表 /
      ``{"answer": "", "followups": []}``），**不能丢**——调用方按轮次顺序拼接
      ``payload["answer"]`` 才是最终正文（与流式模式的 yield 顺序一致）；
    - ``pending_tool_calls`` 为空：本轮是完整回答，``deltas`` 为应用层正文切片、
      ``payload`` 为 ``AnswerGenerationOutput`` 的 dict（与既有非流式返回一致）。
    """

    deltas: list[str]
    payload: dict[str, Any]
    pending_tool_calls: list[dict[str, Any]]
    response_id: str | None


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

    @overload
    async def stream_answer(
        self,
        *,
        answer_context: dict[str, Any],
        tools: None = None,
        previous_response_id: str | None = None,
        tool_outputs: list[dict[str, Any]] | None = None,
    ) -> tuple[list[str], dict[str, Any]]: ...

    @overload
    async def stream_answer(
        self,
        *,
        answer_context: dict[str, Any],
        tools: list[dict[str, Any]],
        previous_response_id: str | None = None,
        tool_outputs: list[dict[str, Any]] | None = None,
    ) -> AnswerToolTurn: ...

    async def stream_answer(
        self,
        *,
        answer_context: dict[str, Any],
        tools: list[dict[str, Any]] | None = None,
        previous_response_id: str | None = None,
        tool_outputs: list[dict[str, Any]] | None = None,
    ) -> tuple[list[str], dict[str, Any]] | AnswerToolTurn:
        """完整校验 Answer 后切分正文，兼容现有应用层 delta 协议。

        - ``tools is None``（或空列表）：既有路径，返回 ``(deltas, payload)``，
          请求里不带 ``tools``，行为与改造前逐字一致；
        - ``tools`` 非空：工具轮（§2.4 D3③），一律返回 :class:`AnswerToolTurn`：
          模型请求工具时 ``pending_tool_calls`` 非空，调用方执行后用
          ``previous_response_id`` + ``tool_outputs`` 续接；模型直接回答时
          ``pending_tool_calls`` 为空。两种情况下 ``deltas`` / ``payload`` 都是
          本轮正文（含工具轮 preamble），调用方按轮次顺序拼接成最终正文。
        """
        _validate_tool_continuation(
            previous_response_id=previous_response_id, tool_outputs=tool_outputs
        )
        if not tools:
            return await self._answer_structured(answer_context=answer_context)
        return await self._answer_tool_turn(
            answer_context=answer_context,
            tools=tools,
            previous_response_id=previous_response_id,
            tool_outputs=tool_outputs,
        )

    async def _answer_structured(
        self, *, answer_context: dict[str, Any]
    ) -> tuple[list[str], dict[str, Any]]:
        """既有非流式路径（§19.2）：完整校验结构后切分正文 delta。"""
        import json as _json

        from backend.conversation.contracts.graph import AnswerGenerationOutput
        from backend.conversation.graph.prompts import ANSWER_SYSTEM_PROMPT

        payload = await self._structured(
            role="answer",
            system_prompt=ANSWER_SYSTEM_PROMPT,
            user_payload=_json.dumps(answer_context, ensure_ascii=False, default=str),
            text_format=AnswerGenerationOutput,
        )
        return _answer_deltas(payload.answer), payload.model_dump(mode="json")

    async def _answer_tool_turn(
        self,
        *,
        answer_context: dict[str, Any],
        tools: list[dict[str, Any]],
        previous_response_id: str | None,
        tool_outputs: list[dict[str, Any]] | None,
    ) -> AnswerToolTurn:
        """非流式工具轮：模型请求工具就回传调用，否则按原路径校验正文。

        与 :meth:`_structured` 一致，正文校验失败最多重试三次；一旦响应里出现
        ``function_call`` 立即返回，**不再重试**（本轮职责已转为等待工具结果）。
        工具轮若同时夹带 preamble 正文（模型先说"我先查一下"再调工具），正文按
        结构化契约解析后一并返回，由调用方按轮次顺序拼接——与流式模式"边生成边
        转发"的结果保持一致。
        """
        import json as _json

        from openai.types.shared import ReasoningEffort
        from openai.types.shared_params import Reasoning

        from backend.conversation.contracts.graph import AnswerGenerationOutput
        from backend.conversation.graph.prompts import (
            ANSWER_MEMORY_TOOLS_INSTRUCTION,
            ANSWER_SYSTEM_PROMPT,
        )

        _validate_tool_continuation(
            previous_response_id=previous_response_id, tool_outputs=tool_outputs
        )
        model = self._model_for("answer")
        system_prompt = ANSWER_SYSTEM_PROMPT + ANSWER_MEMORY_TOOLS_INSTRUCTION
        user_payload = _json.dumps(answer_context, ensure_ascii=False, default=str)
        extra: dict[str, Any] = {"tools": tools}
        if previous_response_id is not None:
            extra["previous_response_id"] = previous_response_id
        last_error: ModelUnavailableError | None = None

        for attempt in range(1, STRUCTURED_MAX_ATTEMPTS + 1):
            # SDK 的 input 是 TypedDict 联合，变量形式无法享受字面量上下文推断，
            # 这里按 Any 传递（与文件内其它 SDK 对象一致）。
            input_items: Any
            if previous_response_id is not None:
                # 续接只送工具结果：system/user 与上一轮输出由服务端上下文携带；
                # 重试约束也无法再注入 system 消息（同请求重试）。
                input_items = _tool_output_items(tool_outputs, logger=self._logger)
            else:
                attempt_prompt = system_prompt
                if attempt > 1:
                    attempt_prompt = f"{system_prompt}\n\n{_RETRY_OUTPUT_CONSTRAINT}"
                input_items = [
                    {"role": "system", "content": attempt_prompt},
                    {"role": "user", "content": user_payload},
                ]
            try:
                response = await self._client.responses.create(
                    model=model,
                    input=input_items,
                    text={"format": _json_schema_format(AnswerGenerationOutput)},
                    max_output_tokens=ROLE_MAX_OUTPUT["answer"],
                    reasoning=Reasoning(
                        effort=cast(ReasoningEffort, self._settings.openai_reasoning_effort)
                    ),
                    timeout=ROLE_TIMEOUTS["answer"],
                    **extra,
                )
            except Exception as exc:
                mapped = _map_conversation_openai_error(exc)
                if not isinstance(mapped, ModelUnavailableError):
                    if mapped is exc:
                        raise
                    raise mapped from exc
                last_error = mapped
                self._log_structured_failure(role="answer", attempt=attempt, error=mapped)
                if attempt >= STRUCTURED_MAX_ATTEMPTS:
                    raise mapped from exc
                continue

            response_id = _string_field(response, "id") or None
            calls = _extract_response_tool_calls(response, logger=self._logger)
            if calls:
                # 工具轮也可能先给一段 preamble 正文（"我先查一下你的学习记录"）：
                # 必须**保留**并交给调用方按轮次顺序拼接，否则"已发出的 delta 一定
                # 是最终正文前缀"这条不变量在流式/非流式之间就不一致（流式本来就
                # 会把工具轮的正事实时 yield 出去）。
                preamble, refusal = _extract_response_text(response)
                if refusal is None and preamble:
                    preamble_deltas, preamble_payload = _tool_turn_content(
                        preamble, text_format=AnswerGenerationOutput
                    )
                else:
                    preamble_deltas, preamble_payload = [], {"answer": "", "followups": []}
                return AnswerToolTurn(
                    deltas=preamble_deltas,
                    payload=preamble_payload,
                    pending_tool_calls=calls,
                    response_id=response_id,
                )
            try:
                payload = _parse_structured_response(
                    response, text_format=AnswerGenerationOutput, attempt=attempt
                )
            except StructuredOutputError as exc:
                last_error = exc
                self._log_structured_failure(role="answer", attempt=attempt, error=exc)
                if attempt >= STRUCTURED_MAX_ATTEMPTS:
                    raise
                continue
            return AnswerToolTurn(
                deltas=_answer_deltas(payload.answer),
                payload=payload.model_dump(mode="json"),
                pending_tool_calls=[],
                response_id=response_id,
            )

        if last_error is not None:  # pragma: no cover - 循环穷尽保护
            raise last_error
        raise ModelUnavailableError("工具轮调用未执行")  # pragma: no cover

    def supports_answer_streaming(self) -> bool:
        """是否启用真实流式回答（默认关闭，由设置控制）。"""
        return bool(self._settings.conversation_answer_streaming)

    def open_answer_stream(
        self,
        *,
        answer_context: dict[str, Any],
        tools: list[dict[str, Any]] | None = None,
        previous_response_id: str | None = None,
        tool_outputs: list[dict[str, Any]] | None = None,
        tool_rounds: int = 0,
    ) -> AnswerTextStream:
        """打开真实流式回答会话（§15.4 流式模式 / §2.4 D3③ 工具轮）。

        由节点迭代消费；流结束或标记解析完成后通过 ``followups`` /
        ``full_text`` 读取结果。不满足启用条件时抛出
        ModelUnavailableError（节点应走非流式回退）。

        - ``tools is None``（或空列表）：既有行为逐字不变，请求不带 ``tools``；
        - ``tools`` 非空：把 ``memory_tool_specs()`` 一类的定义下发给模型。本轮
          结束后从 ``pending_tool_calls`` 取模型请求的调用，由调用方执行后用
          ``previous_response_id``（上一轮 ``response_id``）+ ``tool_outputs``
          续接；``tool_rounds`` 是调用方已发生的工具轮数（§5.7 的 6 次上限校验
          用），本实例在此基础上继续累加。
        """
        import json as _json

        if not self.supports_answer_streaming():
            raise ModelUnavailableError("CONVERSATION_ANSWER_STREAMING 未启用")
        # 续接参数必须成对出现（与 tools 是否启用无关：残缺组合一定是调用方缺陷）。
        _validate_tool_continuation(
            previous_response_id=previous_response_id, tool_outputs=tool_outputs
        )
        return AnswerTextStream(
            client=self._client,
            model=self._model_for("answer"),
            user_payload=_json.dumps(answer_context, ensure_ascii=False, default=str),
            logger=self._logger,
            reasoning_effort=self._settings.openai_reasoning_effort,
            tools=tools or None,
            previous_response_id=previous_response_id,
            tool_outputs=tool_outputs,
            tool_rounds=tool_rounds,
        )

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


# ---------------------------------------------------------------------------
# 工具循环公共件（§2.4 D3③）：调用归一化、结果回灌、续接自检
# ---------------------------------------------------------------------------


def _validate_tool_continuation(
    *, previous_response_id: str | None, tool_outputs: list[dict[str, Any]] | None
) -> None:
    """续接参数自检；两种残缺组合都降级为 ModelUnavailableError（节点回退非流式）。

    ``previous_response_id`` 与 ``tool_outputs`` 必须成对出现：单独给 id 无法说明
    要回灌什么结果，单独给结果则服务端找不到对应的 function_call。
    """
    if previous_response_id is not None and not tool_outputs:
        raise ModelUnavailableError("工具续接缺少 tool_outputs")
    if previous_response_id is None and tool_outputs:
        raise ModelUnavailableError("工具续接缺少 previous_response_id")


def _tool_output_items(
    tool_outputs: list[dict[str, Any]] | None, *, logger: logging.Logger
) -> list[dict[str, Any]]:
    """把调用方执行的工具结果转成 Responses 的 ``function_call_output`` input 项。

    接受 ``{"call_id": str, "output": str | dict}``（图侧 ``memory_tool`` 节点即此形状）：
    ``output`` 为字符串时原样回灌，其它 JSON 可序列化对象按 UTF-8 JSON 序列化
    （``ensure_ascii=False``）。缺 ``call_id`` 或无法序列化的条目**丢弃并记日志**，
    不抛异常（§16.2 降级语义）。
    """
    import json as _json

    items: list[dict[str, Any]] = []
    for entry in tool_outputs or []:
        if not isinstance(entry, dict):
            logger.warning("工具结果不是对象，已丢弃: %s", type(entry).__name__)
            continue
        call_id = str(entry.get("call_id") or "")
        if not call_id:
            logger.warning("工具结果缺少 call_id，已丢弃")
            continue
        payload = entry.get("output")
        if isinstance(payload, str):
            text = payload
        else:
            try:
                text = _json.dumps(payload, ensure_ascii=False, default=str)
            except Exception as exc:  # 序列化失败只丢这一条，不打断整轮
                logger.warning("工具结果序列化失败 call_id=%s: %s", call_id, type(exc).__name__)
                continue
        items.append({"type": "function_call_output", "call_id": call_id, "output": text})
    return items


def _normalize_tool_call(
    *, call_id: str, name: str, arguments: str, logger: logging.Logger
) -> dict[str, Any]:
    """归一化模型请求的工具调用（§2.4 D3③ 冻结结构）。

    - 正常：``{"call_id", "name", "arguments": dict, "executable": True}``，其中
      ``name`` 已从线上函数名映射回 ``memory.search`` / ``memory.read``；
    - ``arguments`` 不是合法 JSON 对象、或缺 call_id / name：标记
      ``executable=False``（``arguments`` 为空 dict，原始串截断后放 ``arguments_raw``
      供诊断），只记日志，**不抛异常**把整轮回答打挂。
    """
    import json as _json

    tool_name = MEMORY_TOOL_CANONICAL_NAMES.get(name, name)
    parsed: Any = None
    failure: str | None = None
    if not arguments.strip():
        # 无参调用：空串按空对象处理（工具本身仍有必填校验）。
        parsed = {}
    else:
        try:
            parsed = _json.loads(arguments)
        except Exception as exc:  # 坏参数只降级不抛错
            failure = f"invalid_json:{type(exc).__name__}"
    if failure is None and not isinstance(parsed, dict):
        failure = "arguments_not_object"
    if failure is None and not call_id:
        failure = "missing_call_id"
    if failure is None and not tool_name:
        failure = "missing_name"
    if failure is not None:
        logger.warning(
            "工具调用不可执行 name=%s call_id=%s reason=%s",
            tool_name or "<空>",
            call_id or "<空>",
            failure,
        )
        return {
            "call_id": call_id,
            "name": tool_name,
            "arguments": {},
            "executable": False,
            "arguments_raw": arguments[:TOOL_ARGUMENTS_RAW_MAX_CHARS],
            "error": failure,
        }
    return {"call_id": call_id, "name": tool_name, "arguments": parsed, "executable": True}


def _extract_response_tool_calls(response: Any, *, logger: logging.Logger) -> list[dict[str, Any]]:
    """从完整 Responses 的 ``output`` 里按序提取 function_call（非流式与终态事件共用）。"""
    calls: list[dict[str, Any]] = []
    for item in _items(_field(response, "output")):
        if _string_field(item, "type") != "function_call":
            continue
        arguments = _field(item, "arguments")
        calls.append(
            _normalize_tool_call(
                call_id=_string_field(item, "call_id"),
                name=_string_field(item, "name"),
                arguments=arguments if isinstance(arguments, str) else "",
                logger=logger,
            )
        )
    return calls


def _answer_deltas(answer: str) -> list[str]:
    """把已校验正文切成应用层 delta（既有 SSE 契约：固定字符窗口）。"""
    return [
        answer[index : index + ANSWER_DELTA_CHARS]
        for index in range(0, len(answer), ANSWER_DELTA_CHARS)
    ]


def _tool_turn_content(
    text: str, *, text_format: type[BaseModel]
) -> tuple[list[str], dict[str, Any]]:
    """工具轮一并给出的正文（preamble）→ (deltas, payload)。

    模型先在工具轮说一句"我先查一下你的学习记录"再调用工具时，这段正文必须保留：
    优先按结构化契约解析（兼容端点可能仍返回契约 JSON），解析不了就原样当正文，
    保证流式/非流式两种模式对同一模型行为给出同一结果。
    """
    try:
        parsed = _parse_lenient(text_format, text)
    except OpenAISchemaInvalidError:
        return _answer_deltas(text), {"answer": text, "followups": []}
    answer = str(_field(parsed, "answer") or "")
    return _answer_deltas(answer), parsed.model_dump(mode="json")


# ---------------------------------------------------------------------------
# 真实流式回答（§15.4 流式模式）
# ---------------------------------------------------------------------------

FOLLOWUPS_OPEN = "<followups>"
FOLLOWUPS_CLOSE = "</followups>"


def _parse_followups(raw: str) -> list[str]:
    """解析 <followups> 收尾标记；任何失败按"无追问"降级（D11）。"""
    import json as _json

    text = raw.strip()
    try:
        parsed = _json.loads(text)
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if isinstance(item, str)][:3]


def _strip_incomplete_marker_prefix(text: str) -> str:
    """剥离正文尾部不完整的 <followups> 标记前缀（最长匹配，仅一次）。"""
    for cut in range(len(FOLLOWUPS_OPEN) - 1, 0, -1):
        if text.endswith(FOLLOWUPS_OPEN[:cut]):
            return text[:-cut]
    return text


class AnswerTextStream:
    """真实流式回答会话（token 级）。

    迭代产出回答正文（不含 followups 收尾标记）；流结束或标记解析完成后可通过
    ``followups`` 读取追问建议、通过 ``full_text`` 读取完整正文。协议：

    - 模型按流式 prompt 输出纯 markdown 正文，最后输出一行
      ``<followups>[...]</followups>`` 收尾标记；
    - 正文 token 实时转发；收尾标记从不进入正文；
    - 标记跨事件边界拆分时，通过保守缓冲避免把半截标记发出去；
    - 只转发 ``response.output_text.delta`` 增量，refusal delta 不会混入正文；
      ``response.completed`` 的 incomplete 状态暴露为 ``truncated``；
    - 全程异常统一映射为 ModelUnavailableError（节点据此决定回退非流式）。

    记忆工具轮（memory-rebuild §2.4 D3③，仅当构造时传入 ``tools``）：

    - 请求额外下发 ``tools``；模型请求工具时累积 ``function_call`` item 的
      ``call_id`` / ``name`` / ``arguments``（``response.output_item.added``、
      ``response.function_call_arguments.delta/.done``），事件全部不进入正文；
    - 本轮结束后从 ``pending_tool_calls`` 读取调用（``arguments`` 已解析成 dict；
      坏 JSON 标 ``executable=False`` 并记日志，不抛异常），由调用方执行；
    - 续接轮用 ``previous_response_id`` + ``tool_outputs``（只送
      ``function_call_output``，system/user 由服务端上下文携带）；
    - 工具轮里模型一并 yield 的正文（preamble）**不算丢弃**：调用方负责把各轮
      正文按轮次顺序拼接成最终正文（每轮续写都是一次新的 ``open_answer_stream``，
      本实例只覆盖一轮），从而保证"已发出的 delta 一定是最终正文的前缀"；
    - ``tools`` 为 None 时不下发 ``tools``、不发续接参数，行为与改造前逐字一致。
    """

    def __init__(
        self,
        *,
        client: Any,
        model: str,
        user_payload: str,
        logger: logging.Logger,
        reasoning_effort: str = "medium",
        tools: list[dict[str, Any]] | None = None,
        previous_response_id: str | None = None,
        tool_outputs: list[dict[str, Any]] | None = None,
        tool_rounds: int = 0,
    ) -> None:
        from backend.conversation.graph.prompts import (
            ANSWER_MEMORY_TOOLS_INSTRUCTION,
            ANSWER_STREAMING_FOLLOWUPS_INSTRUCTION,
            ANSWER_SYSTEM_PROMPT,
        )

        self._client = client
        self._model = model
        self._user_payload = user_payload
        self._logger = logger
        self._reasoning_effort = reasoning_effort
        # 工具引导词只在显式启用工具时拼接，保证 tools=None 的提示词逐字不变。
        system_prompt = ANSWER_SYSTEM_PROMPT
        if tools:
            system_prompt += ANSWER_MEMORY_TOOLS_INSTRUCTION
        self._system_prompt = system_prompt + ANSWER_STREAMING_FOLLOWUPS_INSTRUCTION
        self._tools = list(tools) if tools else None
        self._previous_response_id = previous_response_id
        self._tool_outputs = list(tool_outputs or [])
        self._followups: list[str] = []
        self._full_text = ""
        self._truncated = False
        self._refused = False
        self._response_id: str | None = None
        self._tool_rounds = tool_rounds
        self._pending_tool_calls: list[dict[str, Any]] = []
        #: 原始 function_call 累积（key 为 output_index/item_id），流结束后统一归一化。
        self._tool_call_items: dict[str, dict[str, Any]] = {}

    @property
    def followups(self) -> list[str]:
        return list(self._followups)

    @property
    def full_text(self) -> str:
        return self._full_text

    @property
    def truncated(self) -> bool:
        return self._truncated

    @property
    def refused(self) -> bool:
        return self._refused

    @property
    def pending_tool_calls(self) -> list[dict[str, Any]]:
        """本轮流结束后模型请求的工具调用；空列表 = 不需要工具。

        每项形如 ``{"call_id": str, "name": "memory.search"|"memory.read",
        "arguments": dict, "executable": bool}``：``arguments`` 已从 JSON 字符串
        解析成 dict；解析失败（或缺 call_id / name）时 ``executable=False``、
        ``arguments`` 为空 dict、原始串放在 ``arguments_raw`` 供诊断，同时记录
        告警日志——**不抛异常**把整轮回答打挂。
        """
        return [dict(call) for call in self._pending_tool_calls]

    @property
    def response_id(self) -> str | None:
        """本次响应的 id，供下一轮 ``previous_response_id`` 续接。"""
        return self._response_id

    @property
    def tool_rounds(self) -> int:
        """本 stream 会话已发生的工具轮数（调用方据此做上限校验）。

        计数 = 构造时传入的 ``tool_rounds``（调用方已发生的轮数）+ 本实例中
        模型确实请求过工具的轮数（0 或 1）。
        """
        return self._tool_rounds

    async def __aiter__(self) -> Any:
        from openai import APIConnectionError, APIError, APITimeoutError
        from openai.types.shared import ReasoningEffort
        from openai.types.shared_params import Reasoning

        stream: Any = None
        try:
            # 续接参数必须成对出现；残缺组合直接降级（调用方回退非流式）。
            _validate_tool_continuation(
                previous_response_id=self._previous_response_id, tool_outputs=self._tool_outputs
            )
            # SDK 的 input 是 TypedDict 联合，变量形式无法享受字面量上下文推断，
            # 这里按 Any 传递（与文件内其它 SDK 对象一致）。
            input_items: Any
            extra: dict[str, Any] = {}
            if self._tools is not None:
                extra["tools"] = self._tools
            if self._previous_response_id is not None:
                # 续接轮只送工具结果：上一轮的 system/user 与模型输出由
                # previous_response_id 在服务端上下文里携带，重复下发会污染上下文。
                extra["previous_response_id"] = self._previous_response_id
                input_items = _tool_output_items(self._tool_outputs, logger=self._logger)
                if not input_items:
                    raise ModelUnavailableError("工具续接缺少可用的 tool_outputs")
            else:
                input_items = [
                    {"role": "system", "content": self._system_prompt},
                    {"role": "user", "content": self._user_payload},
                ]
            stream = await self._client.responses.create(
                model=self._model,
                input=input_items,
                max_output_tokens=ROLE_MAX_OUTPUT["answer"],
                timeout=ROLE_TIMEOUTS["answer"],
                reasoning=Reasoning(effort=cast(ReasoningEffort, self._reasoning_effort)),
                stream=True,
                **extra,
            )
        except Exception as exc:
            mapped = _map_conversation_openai_error(exc)
            raise mapped from exc

        pending = ""
        in_tail = False
        try:
            async for event in stream:
                event_type = getattr(event, "type", "") or ""
                # 响应 id：response.created / completed 携带整个响应对象，供续接使用。
                response_id = _string_field(getattr(event, "response", None), "id")
                if response_id:
                    self._response_id = response_id
                # 工具调用事件：只累积调用结构，绝不进入正文（§2.4 D3③）。
                if event_type.startswith("response.function_call_arguments."):
                    self._record_tool_call_arguments(event, done=event_type.endswith(".done"))
                    continue
                if event_type in {"response.output_item.added", "response.output_item.done"}:
                    self._record_tool_call_item(
                        getattr(event, "item", None),
                        output_index=getattr(event, "output_index", None),
                    )
                    continue
                # 状态类事件：refusal 与 completed 先行处理，绝不进入正文。
                if "refusal" in event_type:
                    self._refused = True
                    continue
                if event_type == "response.completed":
                    response_obj = getattr(event, "response", None)
                    status = getattr(response_obj, "status", None)
                    if status == "incomplete":
                        self._truncated = True
                    # 兼容端点兜底：有些实现只在终态事件里带回完整 output。
                    self._record_embedded_tool_calls(response_obj)
                    continue
                # 兼容策略：优先接受官方 response.output_text.delta；
                # 兼容端点（chat-completion-chunk 风格或自定 type 名）事件没有
                # 标准 type，此时按 delta 字段兜底，但拒绝 reasoning 等非正文增量。
                if event_type.startswith("response.reasoning") or event_type.startswith(
                    "response.function_call"
                ):
                    continue
                if event_type != "response.output_text.delta" and not (
                    not event_type and hasattr(event, "delta")
                ):
                    continue
                delta = str(getattr(event, "delta", "") or "")
                if not delta:
                    continue
                pending += delta
                if not in_tail:
                    # 在完整 pending 上搜索标记（标记可能随任意 delta 完成）；
                    # 保留区只用于未命中时的保守 flush，防止半截标记前缀泄漏。
                    idx = pending.find(FOLLOWUPS_OPEN)
                    if idx >= 0:
                        body = pending[:idx]
                        if body:
                            self._full_text += body
                            yield body
                        pending = pending[idx:]
                        in_tail = True
                    else:
                        reserve = len(FOLLOWUPS_OPEN) - 1
                        flush_len = len(pending) - reserve
                        if flush_len > 0:
                            head = pending[:flush_len]
                            self._full_text += head
                            yield head
                            pending = pending[flush_len:]
                if in_tail:
                    close_idx = pending.find(FOLLOWUPS_CLOSE)
                    if close_idx >= 0:
                        raw = pending[len(FOLLOWUPS_OPEN) : close_idx]
                        self._followups = _parse_followups(raw)
                        pending = ""
                        self._finalize_pending_tool_calls()
                        return
            # 流自然结束：尾部若残留不完整的标记前缀，剥离后再作为正文发出
            # （连接中断常走异常路径，此处为低概率兜底）。
            if not in_tail and pending:
                stripped = _strip_incomplete_marker_prefix(pending)
                if stripped != pending:
                    self._logger.warning("answer 流式正文尾部残留标记前缀，已剥离")
                if stripped:
                    self._full_text += stripped
                    yield stripped
            elif in_tail:
                # 标记未闭合：丢弃收尾残片，不进入正文。
                self._logger.warning("answer 流式收尾标记未闭合，followups 按空处理")
            self._finalize_pending_tool_calls()
        except (APIError, APIConnectionError, APITimeoutError, TimeoutError) as exc:
            mapped = _map_conversation_openai_error(exc)
            raise mapped from exc
        finally:
            if stream is not None and hasattr(stream, "close"):
                await stream.close()

    # ------------------------------------------------------------------
    # 工具调用累积（§2.4 D3③）：流事件 → 原始调用项 → 归一化调用列表
    # ------------------------------------------------------------------

    def _tool_call_entry(self, output_index: Any, *, item_id: str) -> dict[str, Any]:
        """按 ``output_index``（同一 item 各事件的稳定键）归并；缺失时退回 item_id。"""
        if isinstance(output_index, int):
            key = f"#{output_index}"
        elif item_id:
            key = item_id
        else:  # pragma: no cover - 官方事件必带 output_index/item_id
            key = f"anon-{len(self._tool_call_items)}"
        return self._tool_call_items.setdefault(key, {"call_id": "", "name": "", "arguments": ""})

    def _record_tool_call_item(self, item: Any, *, output_index: Any) -> None:
        """累积 ``response.output_item.added/.done`` 里的 ``function_call`` item。

        ``.done`` 带完整 arguments，会覆盖 delta 累积值；``.added`` 通常只有空串，
        因此不会与后续 delta 重复拼接。
        """
        if _string_field(item, "type") != "function_call":
            return
        entry = self._tool_call_entry(output_index, item_id=_string_field(item, "id"))
        call_id = _string_field(item, "call_id")
        if call_id:
            entry["call_id"] = call_id
        name = _string_field(item, "name")
        if name:
            entry["name"] = name
        arguments = _field(item, "arguments")
        if isinstance(arguments, str) and arguments:
            entry["arguments"] = arguments

    def _record_tool_call_arguments(self, event: Any, *, done: bool) -> None:
        """累积 ``response.function_call_arguments.delta/.done``。"""
        entry = self._tool_call_entry(
            getattr(event, "output_index", None),
            item_id=_string_field(event, "item_id"),
        )
        name = _string_field(event, "name")
        if name:
            entry["name"] = name
        if done:
            arguments = _field(event, "arguments")
            if isinstance(arguments, str):
                entry["arguments"] = arguments
            return
        delta = _field(event, "delta")
        if isinstance(delta, str) and delta:
            entry["arguments"] = f"{entry.get('arguments') or ''}{delta}"

    def _record_embedded_tool_calls(self, response: Any) -> None:
        """兜底：只在终态事件里带回完整 output 的兼容端点。"""
        if self._tool_call_items:
            return
        for index, item in enumerate(_items(_field(response, "output"))):
            if _string_field(item, "type") != "function_call":
                continue
            arguments = _field(item, "arguments")
            self._tool_call_items[f"embedded-{index}"] = {
                "call_id": _string_field(item, "call_id"),
                "name": _string_field(item, "name"),
                "arguments": arguments if isinstance(arguments, str) else "",
            }

    def _finalize_pending_tool_calls(self) -> None:
        """流结束（含提前遇到收尾标记）后归一化本轮工具调用。"""
        calls = [
            _normalize_tool_call(
                call_id=str(entry.get("call_id") or ""),
                name=str(entry.get("name") or ""),
                arguments=str(entry.get("arguments") or ""),
                logger=self._logger,
            )
            for entry in self._tool_call_items.values()
        ]
        self._pending_tool_calls = calls
        if calls:
            # 只有模型确实请求过工具才计一轮：空轮不计入调用方的上限校验。
            self._tool_rounds += 1
