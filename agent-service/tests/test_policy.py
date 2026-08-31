from __future__ import annotations

from pathlib import Path

import pytest

from autoseguro.domain import SessionStatus
from autoseguro.policy import (
    CedarPolicyEngine,
    PolicyAction,
    PolicyController,
    PolicyEngine,
)
from autoseguro.providers import FakeProvider
from autoseguro.quote_client import QuoteClient
from autoseguro.repository import Repository
from autoseguro.service import AgentService
from autoseguro.telemetry import InMemoryTelemetryBackend, SafeTelemetry

POLICY_DIR = Path(__file__).parents[1] / "policies"


@pytest.fixture
def cedar_engine() -> CedarPolicyEngine:
    return CedarPolicyEngine(
        POLICY_DIR / "autoseguro.cedar",
        POLICY_DIR / "autoseguro.cedarschema",
    )


@pytest.mark.parametrize(
    ("action", "context"),
    [
        (
            PolicyAction.CALL_LLM,
            {"sessionState": "qualification", "sanitized": True, "destination": "openai"},
        ),
        (
            PolicyAction.CALL_LLM,
            {
                "sessionState": "qualification",
                "sanitized": True,
                "destination": "openai-agents-sdk",
            },
        ),
        (
            PolicyAction.CALL_QUOTE,
            {
                "sessionState": "confirmation",
                "dataComplete": True,
                "confirmed": True,
                "sanitized": True,
                "destination": "quote-service",
            },
        ),
        (PolicyAction.PERSIST_AUDIT, {"sanitized": True}),
        (
            PolicyAction.COMPLETE_SESSION,
            {"sessionState": "quote_presented", "quoteSucceeded": True},
        ),
        (PolicyAction.HANDOFF_SESSION, {"reasonPresent": True}),
    ],
)
def test_real_cedar_engine_allows_expected_actions(
    cedar_engine: CedarPolicyEngine,
    action: PolicyAction,
    context: dict[str, object],
) -> None:
    decision = cedar_engine.evaluate(action, "session-1", context)
    assert decision.engine_available
    assert decision.allowed
    assert decision.reasons


@pytest.mark.parametrize(
    "context",
    [
        {
            "sessionState": "qualification",
            "dataComplete": True,
            "confirmed": True,
            "sanitized": True,
            "destination": "quote-service",
        },
        {
            "sessionState": "confirmation",
            "dataComplete": False,
            "confirmed": True,
            "sanitized": True,
            "destination": "quote-service",
        },
        {
            "sessionState": "confirmation",
            "dataComplete": True,
            "confirmed": False,
            "sanitized": True,
            "destination": "quote-service",
        },
        {
            "sessionState": "confirmation",
            "dataComplete": True,
            "confirmed": True,
            "sanitized": False,
            "destination": "quote-service",
        },
        {
            "sessionState": "confirmation",
            "dataComplete": True,
            "confirmed": True,
            "sanitized": True,
            "destination": "untrusted-service",
        },
    ],
)
def test_call_quote_is_default_deny_for_unsafe_context(
    cedar_engine: CedarPolicyEngine, context: dict[str, object]
) -> None:
    decision = cedar_engine.evaluate(PolicyAction.CALL_QUOTE, "session-1", context)
    assert decision.engine_available
    assert not decision.allowed
    assert decision.reasons == ()


def test_shadow_mode_records_deny_without_blocking(cedar_engine: CedarPolicyEngine) -> None:
    telemetry_backend = InMemoryTelemetryBackend()
    controller = PolicyController(
        cedar_engine,
        mode="shadow",
        enforce_actions={PolicyAction.CALL_QUOTE},
        telemetry=SafeTelemetry(telemetry_backend),
    )
    decision = controller.check(
        PolicyAction.CALL_QUOTE,
        "session-1",
        {
            "sessionState": "qualification",
            "dataComplete": False,
            "confirmed": False,
            "sanitized": True,
            "destination": "quote-service",
        },
    )
    assert not decision.allowed
    assert not controller.blocks(PolicyAction.CALL_QUOTE, decision)
    span = telemetry_backend.records[0]
    assert span.name == "cedar_policy_decision"
    assert span.attributes["allowed"] is False
    assert "qualification" not in str(span.attributes)


def test_enforce_mode_blocks_real_cedar_deny(cedar_engine: CedarPolicyEngine) -> None:
    controller = PolicyController(
        cedar_engine,
        mode="enforce",
        enforce_actions={PolicyAction.CALL_QUOTE},
        telemetry=SafeTelemetry(),
    )
    decision = controller.check(
        PolicyAction.CALL_QUOTE,
        "session-1",
        {
            "sessionState": "qualification",
            "dataComplete": False,
            "confirmed": False,
            "sanitized": True,
            "destination": "quote-service",
        },
    )
    assert decision.engine_available
    assert not decision.allowed
    assert controller.blocks(PolicyAction.CALL_QUOTE, decision)


def test_enforce_unavailable_blocks_quote_and_handoffs_without_price(
    repository: Repository, quote_client: QuoteClient
) -> None:
    controller = PolicyController(
        PolicyEngine(),
        mode="enforce",
        enforce_actions={PolicyAction.CALL_QUOTE},
        telemetry=SafeTelemetry(),
    )
    service = AgentService(
        repository,
        FakeProvider(),
        quote_client,
        policy=controller,
    )
    session_id = service.create_session().session.id
    service.handle_message(session_id, "Toyota Corolla 2022")
    service.handle_message(session_id, "Tenho 35 anos e CEP 01310-100")
    service.handle_message(session_id, "Quero o plano completo")
    result = service.handle_message(session_id, "confirmo")

    assert result.session.status is SessionStatus.HANDOFF
    assert result.session.handoff is not None
    assert result.session.handoff.reason == "policy_call_quote_denied"
    assert result.session.quote is None
    assert "R$" not in result.assistant_message.content
    assert service.trace(session_id)["quote_attempts"] == []


def test_invalid_cedar_policy_is_rejected(tmp_path: Path) -> None:
    policy = tmp_path / "invalid.cedar"
    policy.write_text("permit(principal, action, resource) when { missingContext };\n")
    with pytest.raises(ValueError, match="validation failed"):
        CedarPolicyEngine(policy, POLICY_DIR / "autoseguro.cedarschema")
