from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import Any, cast

from .domain import AgentDecision, ExtractedData, SessionState
from .extraction import deterministic_decision
from .telemetry import current_canonical_trace_id

SYSTEM_PROMPT = """Você extrai dados e classifica a intenção em uma conversa de seguro auto.
Nunca calcule, estime ou invente preço. Extraia apenas fatos presentes na mensagem atual.
CEP deve virar somente os dois primeiros dígitos. Datas usam YYYY-MM-DD.
Planos válidos: essencial, completo e premium. Se houver dúvida, deixe o campo nulo.
Intenções: provide_data, confirm, accept, negotiate, human, question ou unknown.
"""
PROMPT_VERSION = "2026-08-28.1"
SCHEMA_VERSION = "agent-decision.v1"
PROVIDER_TIMEOUT_SECONDS = 20.0
PROVIDER_MAX_OUTPUT_TOKENS = 300


class LLMProvider(ABC):
    provider_name = "unknown"
    model = "unknown"

    @abstractmethod
    def generate_decision(
        self,
        *,
        message: str,
        state: SessionState,
        collected: ExtractedData,
    ) -> AgentDecision:
        raise NotImplementedError

    def telemetry_metadata(self) -> dict[str, str | int]:
        return {"provider": self.provider_name, "model": self.model}


class FakeProvider(LLMProvider):
    provider_name = "fake"
    model = "deterministic-v1"

    def generate_decision(
        self,
        *,
        message: str,
        state: SessionState,
        collected: ExtractedData,
    ) -> AgentDecision:
        del state, collected
        return deterministic_decision(message)


class OpenAIProvider(LLMProvider):
    provider_name = "openai"

    def __init__(self, api_key: str, model: str = "gpt-5.4-mini") -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY ausente")
        from openai import OpenAI

        self.client = OpenAI(
            api_key=api_key,
            max_retries=0,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )
        self.model = model
        self._usage: ContextVar[dict[str, int] | None] = ContextVar("openai_usage", default=None)
        self._call_metadata: ContextVar[dict[str, str | int] | None] = ContextVar(
            "openai_call_metadata", default=None
        )

    def generate_decision(
        self,
        *,
        message: str,
        state: SessionState,
        collected: ExtractedData,
    ) -> AgentDecision:
        started = time.monotonic()
        try:
            response = self.client.responses.parse(
                model=self.model,
                instructions=SYSTEM_PROMPT,
                input=(
                    f"Estado: {state.value}\n"
                    f"Dados já confirmados: {collected.model_dump_json()}\n"
                    f"Mensagem atual: {message}"
                ),
                text_format=AgentDecision,
                store=False,
                max_output_tokens=PROVIDER_MAX_OUTPUT_TOKENS,
            )
        except Exception as exc:
            _call_metadata_context(self, "openai_call_metadata").set(
                {
                    "provider_status": "error",
                    "provider_error_category": type(exc).__name__,
                    "provider_duration_ms": round((time.monotonic() - started) * 1000),
                }
            )
            raise
        _call_metadata_context(self, "openai_call_metadata").set(
            {
                "provider_status": "ok",
                "provider_duration_ms": round((time.monotonic() - started) * 1000),
            }
        )
        if response.output_parsed is None:
            raise ValueError("OpenAI retornou resposta sem decisão estruturada")
        usage = getattr(response, "usage", None)
        _usage_context(self, "openai_usage").set(
            {
                "input_tokens": int(usage.input_tokens),
                "output_tokens": int(usage.output_tokens),
                "total_tokens": int(usage.total_tokens),
            }
            if usage is not None
            else {}
        )
        return response.output_parsed

    def telemetry_metadata(self) -> dict[str, str | int]:
        return {
            **super().telemetry_metadata(),
            **(_usage_context(self, "openai_usage").get() or {}),
            **(_call_metadata_context(self, "openai_call_metadata").get() or {}),
        }


class AgentsSDKProvider(LLMProvider):
    """Single-agent structured extractor; it receives no tools, handoffs, or side effects."""

    provider_name = "openai-agents-sdk"

    def __init__(self, api_key: str, model: str = "gpt-5.4-mini") -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY ausente")
        try:
            from agents import Agent
            from agents.models.openai_provider import OpenAIProvider as SDKOpenAIProvider
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "Agents SDK indisponível; instale o extra opcional 'agents'"
            ) from exc

        self.model = model
        openai_client = AsyncOpenAI(
            api_key=api_key,
            max_retries=0,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )
        self._model_provider: Any = SDKOpenAIProvider(
            openai_client=openai_client,
            use_responses=True,
        )
        self._agent: Any = Agent(
            name="autoseguro-decision-extractor",
            instructions=SYSTEM_PROMPT,
            model=model,
            output_type=AgentDecision,
            tools=[],
            handoffs=[],
        )
        self._usage: ContextVar[dict[str, int] | None] = ContextVar(
            "agents_sdk_usage", default=None
        )
        self._call_metadata: ContextVar[dict[str, str | int] | None] = ContextVar(
            "agents_sdk_call_metadata", default=None
        )

    def generate_decision(
        self,
        *,
        message: str,
        state: SessionState,
        collected: ExtractedData,
    ) -> AgentDecision:
        from agents import ModelRetrySettings, ModelSettings, RunConfig, Runner

        trace_id = current_canonical_trace_id()
        started = time.monotonic()
        try:
            result = Runner.run_sync(
                self._agent,
                input=(
                    f"Estado: {state.value}\n"
                    f"Dados já confirmados: {collected.model_dump_json()}\n"
                    f"Mensagem atual: {message}"
                ),
                max_turns=1,
                run_config=RunConfig(
                    model=self.model,
                    model_provider=self._model_provider,
                    model_settings=ModelSettings(
                        max_tokens=PROVIDER_MAX_OUTPUT_TOKENS,
                        store=False,
                        timeout=PROVIDER_TIMEOUT_SECONDS,
                        retry=ModelRetrySettings(max_retries=0),
                    ),
                    tracing_disabled=True,
                    trace_include_sensitive_data=False,
                    workflow_name="autoseguro-decision",
                    trace_id=trace_id,
                    trace_metadata={
                        "prompt_version": PROMPT_VERSION,
                        "schema_version": SCHEMA_VERSION,
                    },
                ),
            )
        except Exception as exc:
            _call_metadata_context(self, "agents_sdk_call_metadata").set(
                {
                    "provider_status": "error",
                    "provider_error_category": type(exc).__name__,
                    "provider_duration_ms": round((time.monotonic() - started) * 1000),
                }
            )
            raise
        _call_metadata_context(self, "agents_sdk_call_metadata").set(
            {
                "provider_status": "ok",
                "provider_duration_ms": round((time.monotonic() - started) * 1000),
            }
        )
        decision = AgentDecision.model_validate(result.final_output)
        usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
        _usage_context(self, "agents_sdk_usage").set(
            {
                "input_tokens": int(usage.input_tokens),
                "output_tokens": int(usage.output_tokens),
                "total_tokens": int(usage.total_tokens),
            }
            if usage is not None
            else {}
        )
        return decision

    def telemetry_metadata(self) -> dict[str, str | int]:
        return {
            **super().telemetry_metadata(),
            **(_usage_context(self, "agents_sdk_usage").get() or {}),
            **(_call_metadata_context(self, "agents_sdk_call_metadata").get() or {}),
            "sdk_tracing": "disabled_no_duplicate_spans",
        }


class AnthropicProvider(LLMProvider):
    provider_name = "anthropic"

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-6") -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY ausente")
        from anthropic import Anthropic

        self.client = Anthropic(
            api_key=api_key,
            max_retries=0,
            timeout=PROVIDER_TIMEOUT_SECONDS,
        )
        self.model = model
        self._usage: ContextVar[dict[str, int] | None] = ContextVar("anthropic_usage", default=None)

    def generate_decision(
        self,
        *,
        message: str,
        state: SessionState,
        collected: ExtractedData,
    ) -> AgentDecision:
        tool: dict[str, Any] = {
            "name": "submit_decision",
            "description": "Retorna a extração e a intenção da mensagem atual.",
            "input_schema": AgentDecision.model_json_schema(),
        }
        response = self.client.messages.create(
            model=self.model,
            max_tokens=PROVIDER_MAX_OUTPUT_TOKENS,
            system=SYSTEM_PROMPT,
            messages=cast(
                Any,
                [
                    {
                        "role": "user",
                        "content": (
                            f"Estado: {state.value}\n"
                            f"Dados já confirmados: {collected.model_dump_json()}\n"
                            f"Mensagem atual: {message}"
                        ),
                    }
                ],
            ),
            tools=cast(Any, [tool]),
            tool_choice=cast(Any, {"type": "tool", "name": "submit_decision"}),
        )
        for block in response.content:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == "submit_decision"
            ):
                usage = getattr(response, "usage", None)
                _usage_context(self, "anthropic_usage").set(
                    {
                        "input_tokens": int(usage.input_tokens),
                        "output_tokens": int(usage.output_tokens),
                        "total_tokens": int(usage.input_tokens + usage.output_tokens),
                    }
                    if usage is not None
                    else {}
                )
                return AgentDecision.model_validate(cast(Any, block).input)
        raise ValueError("Anthropic retornou resposta sem decisão estruturada")

    def telemetry_metadata(self) -> dict[str, str | int]:
        return {
            **super().telemetry_metadata(),
            **(_usage_context(self, "anthropic_usage").get() or {}),
        }


def provider_from_settings(settings: Any) -> LLMProvider:
    provider = settings.llm_provider.lower()
    if provider == "fake":
        return FakeProvider()
    if provider == "openai":
        return OpenAIProvider(settings.openai_api_key or "", settings.llm_model)
    if provider in {"agents_sdk", "openai_agents_sdk"}:
        return AgentsSDKProvider(settings.openai_api_key or "", settings.llm_model)
    if provider == "anthropic":
        model = settings.llm_model
        if model == "gpt-5.4-mini":
            model = "claude-sonnet-4-6"
        return AnthropicProvider(settings.anthropic_api_key or "", model)
    raise ValueError(f"LLM_PROVIDER inválido: {settings.llm_provider}")


def decision_for_log(decision: AgentDecision) -> str:
    return json.dumps(decision.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


def _usage_context(provider: Any, name: str) -> ContextVar[dict[str, int] | None]:
    context = getattr(provider, "_usage", None)
    if context is None:
        context = ContextVar(name, default=None)
        provider._usage = context
    return cast(ContextVar[dict[str, int] | None], context)


def _call_metadata_context(provider: Any, name: str) -> ContextVar[dict[str, str | int] | None]:
    context = getattr(provider, "_call_metadata", None)
    if context is None:
        context = ContextVar(name, default=None)
        provider._call_metadata = context
    return cast(ContextVar[dict[str, str | int] | None], context)
