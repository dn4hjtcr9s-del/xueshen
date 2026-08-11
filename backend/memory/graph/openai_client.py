"""OpenAI Client 边界（§9.1 / §9.2 / §9.4）。

- 通过 Runtime Context 注入，不进入 Graph State。
- 自动化测试与 CI 全部使用 FakeMemoryLLMClient，不要求 OPENAI_API_KEY。
- 日志只记录 prompt_version 与 model_name，不记录完整 Prompt、原始对话和完整模型输出。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, TypeVar, cast

from pydantic import BaseModel

if TYPE_CHECKING:
    from openai.types.responses import ResponseFormatTextJSONSchemaConfigParam

from backend.memory.contracts.errors import (
    OpenAIRateLimitedError,
    OpenAISchemaInvalidError,
    OpenAITimeoutError,
)
from backend.memory.graph.llm_schemas import (
    CandidateExtractionResult,
    MutationPlanResult,
)
from backend.memory.graph.policies import (
    EXTRACT_MAX_OUTPUT_TOKENS,
    PLAN_MAX_OUTPUT_TOKENS,
    LLMCallBudget,
)
from backend.memory.graph.prompt_loader import (
    BUILD_MUTATION_PLAN_PROMPT_VERSION,
    EXTRACT_CANDIDATES_PROMPT_VERSION,
    load_prompt,
)
from backend.settings import Settings

TResult = TypeVar("TResult", bound=BaseModel)


@dataclass(frozen=True)
class LLMCallRecord:
    """每次调用的最小审计信息（§9.4：不记录完整 Prompt/输出）。"""

    prompt_version: str
    model_name: str
    purpose: Literal["extract_candidates", "build_mutation_plan"]


class MemoryLLMClient(Protocol):
    """Memory 专用 LLM 调用边界；返回结构化结果与调用记录。"""

    async def extract_candidates(
        self, *, source_payload: str, budget: LLMCallBudget
    ) -> tuple[CandidateExtractionResult, LLMCallRecord]: ...

    async def build_mutation_plan(
        self, *, plan_payload: str, budget: LLMCallBudget
    ) -> tuple[MutationPlanResult, LLMCallRecord]: ...


class RealMemoryLLMClient:
    """真实 AsyncOpenAI 实现：Responses API + Structured Outputs（§9.1）。"""

    def __init__(self, *, settings: Settings) -> None:
        if not settings.openai_api_key:
            raise ValueError("RealMemoryLLMClient 需要 OPENAI_API_KEY")
        from openai import AsyncOpenAI

        self._settings = settings
        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
            timeout=settings.openai_memory_timeout_seconds,
        )

    async def _parse(
        self,
        *,
        prompt_version: str,
        purpose: Literal["extract_candidates", "build_mutation_plan"],
        user_payload: str,
        text_format: type[TResult],
        max_output_tokens: int,
        budget: LLMCallBudget,
    ) -> tuple[TResult, LLMCallRecord]:
        from openai.types.shared import ReasoningEffort
        from openai.types.shared_params import Reasoning

        budget.consume()
        try:
            # 用 create + 显式 text format 而非 SDK parse()：后者会在 post-parser 中
            # 急切校验 JSON，DeepSeek 等兼容端点偶发 Markdown 围栏时直接抛错，
            # 应用层拿不到原始文本无法兜底。create 只负责传输，解析由 _parse_lenient 完成。
            response = await self._client.responses.create(
                model=self._settings.openai_memory_model,
                input=[
                    {"role": "system", "content": load_prompt(prompt_version)},
                    {"role": "user", "content": user_payload},
                ],
                text={"format": _json_schema_format(text_format)},
                max_output_tokens=max_output_tokens,
                reasoning=Reasoning(
                    effort=cast(ReasoningEffort, self._settings.openai_reasoning_effort)
                ),
            )
        except Exception as exc:
            raise _map_openai_error(exc) from exc
        parsed = _parse_lenient(text_format, response.output_text or "")
        record = LLMCallRecord(
            prompt_version=prompt_version,
            model_name=self._settings.openai_memory_model,
            purpose=purpose,
        )
        return parsed, record

    async def extract_candidates(
        self, *, source_payload: str, budget: LLMCallBudget
    ) -> tuple[CandidateExtractionResult, LLMCallRecord]:
        return await self._parse(
            prompt_version=EXTRACT_CANDIDATES_PROMPT_VERSION,
            purpose="extract_candidates",
            user_payload=source_payload,
            text_format=CandidateExtractionResult,
            max_output_tokens=EXTRACT_MAX_OUTPUT_TOKENS,
            budget=budget,
        )

    async def build_mutation_plan(
        self, *, plan_payload: str, budget: LLMCallBudget
    ) -> tuple[MutationPlanResult, LLMCallRecord]:
        return await self._parse(
            prompt_version=BUILD_MUTATION_PLAN_PROMPT_VERSION,
            purpose="build_mutation_plan",
            user_payload=plan_payload,
            text_format=MutationPlanResult,
            max_output_tokens=PLAN_MAX_OUTPUT_TOKENS,
            budget=budget,
        )


def _json_schema_format(
    text_format: type[BaseModel],
) -> ResponseFormatTextJSONSchemaConfigParam:
    """构造 Responses API 的 json_schema text format（等价 SDK parse 的请求侧行为）。"""
    from openai.lib._pydantic import to_strict_json_schema

    return {
        "type": "json_schema",
        "name": text_format.__name__,
        "schema": to_strict_json_schema(text_format),
        "strict": True,
    }


def _parse_lenient[T: BaseModel](text_format: type[T], raw: str) -> T:
    """兜底解析：剥离 Markdown 代码围栏后按 Schema 校验（DeepSeek 等兼容端点偶发）。"""
    text = raw.strip()
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return text_format.model_validate_json(text)
    except Exception as exc:
        raise OpenAISchemaInvalidError(
            f"模型输出无法解析为结构化 Schema: {str(exc)[:200]}"
        ) from exc


def _map_openai_error(exc: Exception) -> Exception:
    """SDK 异常 → 契约错误（§11.1 重试分类）。"""
    from openai import APIError, APITimeoutError, RateLimitError

    if isinstance(exc, APITimeoutError | TimeoutError):
        return OpenAITimeoutError(str(exc)[:200])
    if isinstance(exc, RateLimitError):
        return OpenAIRateLimitedError(str(exc)[:200])
    if isinstance(exc, APIError):
        return OpenAISchemaInvalidError(str(exc)[:200])
    return exc


@dataclass
class FakeMemoryLLMClient:
    """测试用 Client：脚本化结果队列，记录调用，执行预算（§9.1 裁决）。"""

    extract_queue: list[CandidateExtractionResult | Exception] = field(default_factory=list)
    plan_queue: list[MutationPlanResult | Exception] = field(default_factory=list)
    records: list[LLMCallRecord] = field(default_factory=list)
    model_name: str = "fake-memory-model"

    def _pop(
        self,
        queue: list[TResult | Exception],
        *,
        prompt_version: str,
        purpose: Literal["extract_candidates", "build_mutation_plan"],
        budget: LLMCallBudget,
    ) -> tuple[TResult, LLMCallRecord]:
        budget.consume()
        self.records.append(
            LLMCallRecord(
                prompt_version=prompt_version, model_name=self.model_name, purpose=purpose
            )
        )
        if not queue:
            raise OpenAISchemaInvalidError(f"Fake 队列已空: {purpose}")
        result = queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result, self.records[-1]

    async def extract_candidates(
        self, *, source_payload: str, budget: LLMCallBudget
    ) -> tuple[CandidateExtractionResult, LLMCallRecord]:
        return self._pop(
            self.extract_queue,
            prompt_version=EXTRACT_CANDIDATES_PROMPT_VERSION,
            purpose="extract_candidates",
            budget=budget,
        )

    async def build_mutation_plan(
        self, *, plan_payload: str, budget: LLMCallBudget
    ) -> tuple[MutationPlanResult, LLMCallRecord]:
        return self._pop(
            self.plan_queue,
            prompt_version=BUILD_MUTATION_PLAN_PROMPT_VERSION,
            purpose="build_mutation_plan",
            budget=budget,
        )
