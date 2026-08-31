from types import SimpleNamespace
from unittest.mock import Mock, patch

from autoseguro.domain import AgentDecision, ExtractedData, Intent, SessionState
from autoseguro.providers import AgentsSDKProvider, AnthropicProvider, OpenAIProvider
from autoseguro.telemetry import canonical_trace_id, telemetry_context

DECISION = AgentDecision(
    intent=Intent.PROVIDE_DATA,
    extracted=ExtractedData(age=35),
    confidence=0.95,
)


def test_openai_adapter_uses_structured_response() -> None:
    provider = object.__new__(OpenAIProvider)
    provider.model = "gpt-5.4-mini"
    provider.client = SimpleNamespace(
        responses=SimpleNamespace(parse=Mock(return_value=SimpleNamespace(output_parsed=DECISION)))
    )
    result = provider.generate_decision(
        message="Tenho 35 anos",
        state=SessionState.QUALIFICATION,
        collected=ExtractedData(),
    )
    assert result == DECISION
    assert provider.client.responses.parse.call_args.kwargs["store"] is False
    assert provider.client.responses.parse.call_args.kwargs["max_output_tokens"] == 300
    assert provider.telemetry_metadata()["provider_status"] == "ok"


def test_anthropic_adapter_uses_forced_tool() -> None:
    provider = object.__new__(AnthropicProvider)
    provider.model = "claude-sonnet-4-6"
    block = SimpleNamespace(type="tool_use", name="submit_decision", input=DECISION.model_dump())
    provider.client = SimpleNamespace(
        messages=SimpleNamespace(create=Mock(return_value=SimpleNamespace(content=[block])))
    )
    result = provider.generate_decision(
        message="Tenho 35 anos",
        state=SessionState.QUALIFICATION,
        collected=ExtractedData(),
    )
    assert result == DECISION
    assert (
        provider.client.messages.create.call_args.kwargs["tool_choice"]["name"] == "submit_decision"
    )


def test_agents_sdk_adapter_is_structured_and_has_no_side_effect_tools() -> None:
    from agents import Runner

    usage = SimpleNamespace(input_tokens=10, output_tokens=4, total_tokens=14)
    provider = AgentsSDKProvider("test-key", "gpt-5.4-mini")
    assert provider._agent.output_type is AgentDecision
    assert provider._agent.tools == []
    assert provider._agent.handoffs == []

    result = SimpleNamespace(
        final_output=DECISION,
        context_wrapper=SimpleNamespace(usage=usage),
    )
    correlation_id = "agents-sdk-correlation"
    with (
        patch.object(Runner, "run_sync", return_value=result) as run_sync,
        telemetry_context(
            correlation_id=correlation_id,
            session_id="11111111-1111-1111-1111-111111111111",
        ),
    ):
        decision = provider.generate_decision(
            message="Tenho 35 anos",
            state=SessionState.QUALIFICATION,
            collected=ExtractedData(),
        )

    assert decision == DECISION
    kwargs = run_sync.call_args.kwargs
    assert kwargs["max_turns"] == 1
    assert kwargs["run_config"].tracing_disabled is True
    assert kwargs["run_config"].trace_include_sensitive_data is False
    assert kwargs["run_config"].trace_id == canonical_trace_id(correlation_id)
    assert kwargs["run_config"].model_settings.max_tokens == 300
    assert kwargs["run_config"].model_settings.store is False
    assert kwargs["run_config"].model_settings.timeout == 20.0
    assert kwargs["run_config"].model_settings.retry.max_retries == 0
    assert provider.telemetry_metadata()["total_tokens"] == 14
    assert provider.telemetry_metadata()["provider_status"] == "ok"
