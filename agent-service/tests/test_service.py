from __future__ import annotations

from autoseguro.domain import SessionState, SessionStatus
from autoseguro.providers import FakeProvider
from autoseguro.quote_client import QuoteClient
from autoseguro.repository import Repository
from autoseguro.service import AgentService


def test_happy_path_persists_sanitized_trace(service: AgentService) -> None:
    result = service.create_session()
    session_id = result.session.id
    service.handle_message(session_id, "Meu veículo é um Toyota Corolla 2022")
    service.handle_message(
        session_id,
        "Tenho 35 anos, CEP 01310-100, CPF 389.083.863-43 e email teste@example.com",
    )
    service.handle_message(session_id, "Quero o plano completo")
    quoted = service.handle_message(session_id, "confirmo")

    assert quoted.session.state is SessionState.QUOTE_PRESENTED
    assert quoted.session.quote is not None
    assert quoted.session.quote["premio_mensal"] == 209.9
    assert "R$ 209,90" in quoted.assistant_message.content
    stored_text = " ".join(message.content for message in quoted.session.messages)
    assert "389.083.863-43" not in stored_text
    assert "teste@example.com" not in stored_text
    assert "01310-100" not in stored_text
    trace = service.trace(session_id)
    assert trace["quote_attempts"][0]["status"] == "success"  # type: ignore[index]

    completed = service.handle_message(session_id, "fechado")
    assert completed.session.status is SessionStatus.COMPLETED


def test_media_goes_to_handoff(service: AgentService) -> None:
    session_id = service.create_session().session.id
    result = service.handle_message(session_id, "[audio]", "audio")
    assert result.session.status is SessionStatus.HANDOFF
    assert result.session.handoff is not None
    assert result.session.handoff.reason == "unsupported_media"


def test_two_ambiguous_messages_go_to_handoff(service: AgentService) -> None:
    session_id = service.create_session().session.id
    service.handle_message(session_id, "oi")
    result = service.handle_message(session_id, "não sei")
    assert result.session.status is SessionStatus.HANDOFF
    assert result.session.handoff is not None
    assert result.session.handoff.reason == "persistent_ambiguity"


def test_explicit_human_request_goes_to_handoff(service: AgentService) -> None:
    session_id = service.create_session().session.id
    result = service.handle_message(session_id, "Quero falar com um atendente humano")
    assert result.session.status is SessionStatus.HANDOFF
    assert result.session.handoff is not None
    assert result.session.handoff.reason == "human_requested"


def test_negotiation_after_quote_goes_to_handoff_without_new_price(service: AgentService) -> None:
    session_id = service.create_session().session.id
    service.handle_message(session_id, "Toyota Corolla 2022")
    service.handle_message(session_id, "Tenho 35 anos e CEP 01310-100")
    service.handle_message(session_id, "Quero o plano completo")
    quoted = service.handle_message(session_id, "confirmo")
    assert quoted.session.state is SessionState.QUOTE_PRESENTED
    result = service.handle_message(session_id, "Quero desconto")
    assert result.session.status is SessionStatus.HANDOFF
    assert result.session.handoff is not None
    assert result.session.handoff.reason == "commercial_negotiation"
    assert "R$" not in result.assistant_message.content


def test_session_message_limit_forces_handoff(
    repository: Repository, quote_client: QuoteClient
) -> None:
    service = AgentService(
        repository,
        FakeProvider(),
        quote_client,
        max_messages_per_session=10,
    )
    session_id = service.create_session().session.id
    for index in range(9):
        repository.add_message(session_id, "assistant", "text", f"mensagem {index}")

    result = service.handle_message(session_id, "Toyota Corolla 2022")

    assert result.session.status is SessionStatus.HANDOFF
    assert result.session.handoff is not None
    assert result.session.handoff.reason == "session_limit_reached"
    assert len(result.session.messages) <= 10
