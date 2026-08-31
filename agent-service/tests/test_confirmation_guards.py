from __future__ import annotations

import httpx
import pytest

from autoseguro.domain import AgentDecision, ExtractedData, Intent, SessionState, SessionStatus
from autoseguro.providers import FakeProvider, LLMProvider
from autoseguro.quote_client import QuoteClient
from autoseguro.repository import Repository
from autoseguro.service import AgentService


def _prepare(service: AgentService) -> str:
    session_id = service.create_session().session.id
    service.handle_message(session_id, "Toyota Corolla 2022")
    service.handle_message(session_id, "Tenho 35 anos e CEP 01310-100")
    service.handle_message(session_id, "Quero o plano completo")
    return session_id


@pytest.mark.parametrize(
    "message",
    ["Não aceito a proposta", "Não quero contratar", "Não quero fechar"],
)
def test_negative_acceptance_never_completes(service: AgentService, message: str) -> None:
    session_id = _prepare(service)
    service.handle_message(session_id, "confirmo os dados")
    result = service.handle_message(session_id, message)
    assert result.session.status is SessionStatus.ACTIVE
    assert result.session.state is SessionState.QUOTE_PRESENTED


def test_negative_confirmation_never_calls_quote(repository: Repository) -> None:
    calls = 0

    def quote(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    service = AgentService(
        repository,
        FakeProvider(),
        QuoteClient("http://quote.test", transport=httpx.MockTransport(quote)),
    )
    session_id = _prepare(service)
    result = service.handle_message(session_id, "Não confirmo")
    assert result.session.state is SessionState.CONFIRMATION
    assert calls == 0


def test_corrections_win_over_positive_terms(service: AgentService) -> None:
    session_id = _prepare(service)
    updated = service.handle_message(session_id, "Sim, mas minha idade é 42")
    assert updated.session.state is SessionState.CONFIRMATION
    assert updated.session.collected.age == 42
    assert updated.session.quote is None


def test_plan_and_age_corrections_are_last_explicit_value(service: AgentService) -> None:
    session_id = service.create_session().session.id
    service.handle_message(session_id, "Toyota Corolla 2022")
    service.handle_message(session_id, "Não tenho 35 anos; tenho 42 anos e CEP 01310-100")
    result = service.handle_message(session_id, "Não quero o plano completo; quero o premium")
    assert result.session.collected.age == 42
    assert result.session.collected.plan_id == "premium"


class _MutatingConfirmProvider(LLMProvider):
    provider_name = "test"
    model = "test"

    def generate_decision(
        self, *, message: str, state: SessionState, collected: ExtractedData
    ) -> AgentDecision:
        if state is SessionState.CONFIRMATION:
            return AgentDecision(intent=Intent.CONFIRM, extracted=ExtractedData(age=40))
        return FakeProvider().generate_decision(message=message, state=state, collected=collected)


def test_llm_data_on_confirm_turn_requires_new_confirmation(
    repository: Repository, quote_client: QuoteClient
) -> None:
    service = AgentService(repository, _MutatingConfirmProvider(), quote_client)
    session_id = _prepare(service)
    result = service.handle_message(session_id, "confirmo os dados")
    assert result.session.state is SessionState.CONFIRMATION
    assert result.session.collected.age == 40
    assert result.session.quote is None
    assert not repository.get_session(session_id).quote_attempts
