"""Study OpenAI Gateway（方案 §19/§9.2/D15，v1.2）。

- 不写死模型名：按角色从 Settings 读取 OPENAI_STUDY_INTAKE_MODEL /
  OPENAI_STUDY_PLAN_MODEL / OPENAI_STUDY_FEED_MODEL；
- 每次调用先按 (user_id, purpose, input_hash, prompt_version, model, schema_version)
  查 study_model_call_records，命中已验证响应直接复用（§15.2，重放不重复计费）；
- 模型输出必须通过 Pydantic/JSON Schema 严格校验（§18.7），失败分类记录；
- 解析复用 Memory 域 lenient 工具；普通日志不落完整响应（§18.5）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from uuid import UUID, uuid4

from openai import APITimeoutError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.memory.contracts.errors import OpenAISchemaInvalidError
from backend.memory.graph.openai_client import _parse_lenient
from backend.settings import Settings
from backend.study.contracts.errors import StudyPlanGenerationFailedError
from backend.study.persistence import repositories as repo
from backend.study.services.idempotency import request_hash

ROLE_MODELS = {
    "intake": "openai_study_intake_model",
    "plan": "openai_study_plan_model",
    "feed": "openai_study_feed_model",
}

#: stale running 缓存行的回收阈值（超过后允许重试，防止进程崩溃毒化 30 天）
STALE_RUNNING_SECONDS = 600.0


def input_hash_of(*parts: Any) -> str:
    """结构化输入哈希（§15.2：规范化 JSON 的 sha256，与幂等 hash 同构）。"""
    return request_hash(list(parts))


class StudyOpenAIGateway:
    """Study 域 OpenAI Gateway（Real 实现；测试注入 FakeStudyOpenAI）。"""

    def __init__(self, *, settings: Settings, logger: logging.Logger | None = None) -> None:
        if not settings.openai_api_key:
            raise ValueError("StudyOpenAIGateway 需要 OPENAI_API_KEY")
        from openai import AsyncOpenAI

        self._settings = settings
        self._logger = logger or logging.getLogger("study.gateways.openai")
        self._client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url or None,
        )
        # §19/D10：intake 超时必须来自 STUDY_INTAKE_REQUEST_TIMEOUT_SECONDS
        self._timeouts: dict[str, float] = {
            "intake": settings.study_intake_request_timeout_seconds,
            "plan": settings.study_operation_soft_timeout_seconds,
            "feed": 60.0,
        }

    def model_for(self, purpose: str) -> str:
        model = getattr(self._settings, ROLE_MODELS[purpose], "")
        if not model:
            raise StudyPlanGenerationFailedError(f"未配置 {ROLE_MODELS[purpose]} 环境变量")
        return model

    async def structured_call(
        self,
        *,
        session: AsyncSession,
        user_id: UUID,
        operation_id: UUID | None,
        purpose: str,
        prompt_version: str,
        system_prompt: str,
        user_payload: Any,
        text_format: type[Any],
        cache_retention_days: int,
        now: Any,
        schema_version: str = "1",
    ) -> Any:
        """带缓存的严格结构化调用（§15.2/D15）。

        返回校验通过的模型对象；非法输出抛 OpenAISchemaInvalidError，
        Study 节点据此走修复/失败分支。
        """
        model = self.model_for(purpose)
        input_hash = input_hash_of(
            purpose, prompt_version, schema_version, system_prompt, user_payload
        )
        cached = await repo.get_model_call_row(
            session,
            user_id=user_id,
            purpose=purpose,
            input_hash=input_hash,
            prompt_version=prompt_version,
            model=model,
            schema_version=schema_version,
        )
        if cached is not None and cached["status"] == "succeeded" and cached["validated_response"]:
            self._logger.info("模型响应缓存命中 purpose=%s", purpose)
            return text_format.model_validate(cached["validated_response"])
        if cached is not None and cached["status"] == "running":
            age = (now - cached["created_at"]).total_seconds()
            if age <= STALE_RUNNING_SECONDS:
                raise StudyPlanGenerationFailedError("同输入模型调用仍在执行")
            # 进程崩溃遗留的 running 行：标记失败后允许重试（评审必改 #1）
            self._logger.warning("回收 stale running 模型缓存 purpose=%s", purpose)
            await repo.update_model_call_result(
                session,
                model_call_id=UUID(str(cached["model_call_id"])),
                status="failed",
                validated_response=None,
                error_code="STALE_RUNNING",
                expected_status="running",
            )
            cached = await repo.get_model_call_row(
                session,
                user_id=user_id,
                purpose=purpose,
                input_hash=input_hash,
                prompt_version=prompt_version,
                model=model,
                schema_version=schema_version,
            )

        if cached is not None and cached["status"] == "failed":
            # 唯一键只有一行：回收失败行复用其 model_call_id 重试
            record_id = UUID(str(cached["model_call_id"]))
            await repo.update_model_call_result(
                session,
                model_call_id=record_id,
                status="running",
                validated_response=None,
                error_code=None,
                expected_status="failed",
            )
            inserted = True
        else:
            record_id = uuid4()
            inserted = await repo.insert_model_call_row(
                session,
                model_call_id=record_id,
                user_id=user_id,
                operation_id=operation_id,
                purpose=purpose,
                input_hash=input_hash,
                prompt_version=prompt_version,
                model=model,
                schema_version=schema_version,
                expires_at=repo.model_cache_expiry(now, cache_retention_days),
            )
        if not inserted:
            # 并发同键：重查缓存
            cached = await repo.get_model_call_row(
                session,
                user_id=user_id,
                purpose=purpose,
                input_hash=input_hash,
                prompt_version=prompt_version,
                model=model,
                schema_version=schema_version,
            )
            if cached is not None and cached["status"] == "succeeded":
                assert cached["validated_response"] is not None
                return text_format.model_validate(cached["validated_response"])
            if cached is not None and cached["status"] == "failed":
                # 竞争失败行：复用同一唯一键记录继续执行
                record_id = UUID(str(cached["model_call_id"]))
                await repo.update_model_call_result(
                    session,
                    model_call_id=record_id,
                    status="running",
                    validated_response=None,
                    error_code=None,
                    expected_status="failed",
                )
            else:
                raise StudyPlanGenerationFailedError("模型调用并发冲突，请重试")

        raw: Any = None
        usage: dict[str, Any] = {}
        try:
            from typing import cast

            from openai.types.shared import ReasoningEffort
            from openai.types.shared_params import Reasoning

            response = await asyncio.wait_for(
                self._client.responses.create(
                    model=model,
                    input=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
                    ],
                    text={"format": _json_schema_format(text_format)},
                    max_output_tokens=2000,
                    reasoning=Reasoning(
                        effort=cast(ReasoningEffort, self._settings.openai_reasoning_effort)
                    ),
                ),
                timeout=self._timeouts.get(purpose, 60.0),
            )
            raw = response.output_text or ""
            if response.usage is not None:
                usage = {
                    "input_tokens": getattr(response.usage, "input_tokens", 0) or 0,
                    "output_tokens": getattr(response.usage, "output_tokens", 0) or 0,
                }
            parsed = _parse_lenient(text_format, raw)
            validated = parsed.model_dump(mode="json")
            await repo.update_model_call_result(
                session,
                model_call_id=record_id,
                status="succeeded",
                validated_response=validated,
                usage=usage,
                expected_status="running",
            )
            return parsed
        except OpenAISchemaInvalidError:
            await repo.update_model_call_result(
                session,
                model_call_id=record_id,
                status="failed",
                validated_response=None,
                usage=usage,
                error_code="SCHEMA_INVALID",
                expected_status="running",
            )
            raise
        except TimeoutError as exc:
            await repo.update_model_call_result(
                session,
                model_call_id=record_id,
                status="failed",
                validated_response=None,
                usage=usage,
                error_code="TIMEOUT",
                expected_status="running",
            )
            # §9.1/D10：超时返回 retryable 503，而不是 500
            raise StudyPlanGenerationFailedError(
                f"模型调用超时（{self._timeouts.get(purpose, 60.0)}s），请重试"
            ) from exc
        except APITimeoutError as exc:
            # OpenAI SDK 自身超时同样映射 retryable 503（次要路径不裸抛 500）
            await repo.update_model_call_result(
                session,
                model_call_id=record_id,
                status="failed",
                validated_response=None,
                usage=usage,
                error_code="API_TIMEOUT",
                expected_status="running",
            )
            raise StudyPlanGenerationFailedError("OpenAI 请求超时，请重试") from exc
        except Exception as exc:
            await repo.update_model_call_result(
                session,
                model_call_id=record_id,
                status="failed",
                validated_response=None,
                usage=usage,
                error_code=type(exc).__name__,
                expected_status="running",
            )
            raise


def _json_schema_format(model: type[Any]) -> Any:
    """Responses API 的 json_schema text format（复用 Memory 域工具）。"""
    from backend.memory.graph.openai_client import _json_schema_format as _fmt

    return _fmt(model)
