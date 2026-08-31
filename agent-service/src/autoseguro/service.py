from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from .domain import (
    AgentDecision,
    ExtractedData,
    HandoffInfo,
    Intent,
    MessageView,
    QuoteAttempt,
    SessionState,
    SessionStatus,
    SessionView,
)
from .errors import SessionConflictError
from .extraction import deterministic_extract, deterministic_intent
from .pii import redact_pii
from .policy import PolicyAction, PolicyController, PolicyEngine
from .providers import PROMPT_VERSION, SCHEMA_VERSION, LLMProvider
from .quote_client import QuoteClient, QuoteInvalid, QuoteRejected, QuoteUnavailable
from .repository import Repository
from .security import hash_session_token, new_session_token
from .telemetry import SafeTelemetry

logger = logging.getLogger(__name__)

GREETING = (
    "Olá! Posso qualificar seu perfil e consultar uma cotação oficial da AutoSeguro. "
    "Para começar, qual é o modelo e o ano do veículo? Não envie CPF, telefone ou documentos."
)


class TurnResult(BaseModel):
    session: SessionView
    assistant_message: MessageView


class CreateSessionResult(TurnResult):
    session_token: str


class AgentService:
    def __init__(
        self,
        repository: Repository,
        provider: LLMProvider,
        quote_client: QuoteClient,
        *,
        max_messages_per_session: int = 100,
        telemetry: SafeTelemetry | None = None,
        policy: PolicyController | None = None,
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.quote_client = quote_client
        self.max_messages_per_session = max_messages_per_session
        self.telemetry = telemetry or SafeTelemetry()
        self.policy = policy or PolicyController(
            PolicyEngine(),
            mode="off",
            enforce_actions=set(),
            telemetry=self.telemetry,
        )

    def create_session(self) -> CreateSessionResult:
        token = new_session_token()
        record = self.repository.create_session(hash_session_token(token))
        message = self.repository.add_message(record.id, "assistant", "text", GREETING)
        return CreateSessionResult(
            session=self.get_session(record.id),
            assistant_message=_message_view(message),
            session_token=token,
        )

    def get_session(self, session_id: str) -> SessionView:
        record = self.repository.get_session(session_id)
        handoff = None
        if record.handoff:
            handoff = HandoffInfo(reason=record.handoff.reason, summary=record.handoff.summary)
        return SessionView(
            id=record.id,
            status=SessionStatus(record.status),
            state=SessionState(record.state),
            collected=ExtractedData.model_validate(record.collected),
            messages=[
                _message_view(item) for item in record.messages[-self.max_messages_per_session :]
            ],
            quote=record.quote_payload,
            handoff=handoff,
        )

    def handle_message(
        self, session_id: str, content: str, message_type: str = "text"
    ) -> TurnResult:
        record = self.repository.get_session(session_id)
        if record.status != SessionStatus.ACTIVE.value:
            raise SessionConflictError()

        if len(record.messages) >= self.max_messages_per_session:
            collected = ExtractedData.model_validate(record.collected)
            return self._handoff(
                session_id,
                "session_limit_reached",
                collected,
                "A sessão atingiu o limite operacional. Vou encaminhar para um atendente.",
            )

        with self.telemetry.span(
            "pii_redaction", {"session_id": session_id, "message_type": message_type}
        ) as redaction_span:
            sanitized = redact_pii(content)
            redaction_span.set_attribute("redaction_applied", sanitized != content)
        self.policy.check(
            PolicyAction.PERSIST_AUDIT,
            session_id,
            {"sanitized": True},
        )
        self.repository.add_message(session_id, "lead", message_type, sanitized)
        state = SessionState(record.state)
        collected = ExtractedData.model_validate(record.collected)

        if message_type != "text":
            return self._handoff(
                session_id,
                "unsupported_media",
                collected,
                "Recebi uma mídia que este canal não transcreve com segurança. "
                "Vou encaminhar para um atendente.",
            )

        decision = self._decide(session_id, sanitized, content, state, collected)
        if decision.intent is Intent.HUMAN:
            return self._handoff(
                session_id,
                "human_requested",
                collected,
                "Claro. Vou encaminhar a conversa para um atendente humano.",
            )
        if state is SessionState.QUOTE_PRESENTED:
            if decision.intent is Intent.ACCEPT or decision.intent is Intent.CONFIRM:
                return self._complete(session_id)
            if decision.intent is Intent.NEGOTIATE:
                return self._handoff(
                    session_id,
                    "commercial_negotiation",
                    collected,
                    "Um atendente precisa avaliar condições comerciais e alternativas com você.",
                )
            return self._reply(
                session_id,
                "Posso registrar seu interesse nesta proposta ou encaminhar você "
                "para conversar com um atendente.",
            )

        merged = collected.merge(decision.extracted)
        changed = merged != collected
        clarification_count = 0 if changed else record.clarification_count + 1
        self._update_session(
            session_id,
            collected=merged,
            clarification_count=clarification_count,
        )

        if clarification_count >= 2:
            return self._handoff(
                session_id,
                "persistent_ambiguity",
                merged,
                "Não consegui confirmar os dados necessários com segurança. "
                "Vou pedir ajuda a um atendente.",
            )

        missing = _missing_fields(merged)
        if missing:
            next_state = (
                SessionState.PLAN_SELECTION if missing[0] == "plano" else SessionState.QUALIFICATION
            )
            self._update_session(session_id, state=next_state, event=f"missing_{missing[0]}")
            return self._reply(session_id, _question_for(missing[0]))

        if state is SessionState.CONFIRMATION and decision.intent is Intent.CONFIRM:
            return self._run_quote(session_id, merged)

        self._update_session(session_id, state=SessionState.CONFIRMATION, event="data_complete")
        return self._reply(session_id, _confirmation_message(merged))

    def trace(self, session_id: str) -> dict[str, Any]:
        record = self.repository.get_session(session_id)
        technical_events = self.repository.list_trace_events(session_id)
        return {
            "session_id": record.id,
            "status": record.status,
            "state": record.state,
            "transitions": [
                {
                    "from": item.from_state,
                    "to": item.to_state,
                    "event": item.event,
                    "created_at": item.created_at.isoformat(),
                }
                for item in record.transitions
            ],
            "quote_attempts": [
                {
                    "quote_id": item.quote_id,
                    "attempt_no": item.attempt_no,
                    "status": item.status,
                    "http_status": item.http_status,
                    "duration_ms": item.duration_ms,
                    "error_code": item.error_code,
                }
                for item in record.quote_attempts
            ],
            "handoff": (
                {"reason": record.handoff.reason, "summary": record.handoff.summary}
                if record.handoff
                else None
            ),
            "technical_events": [
                {
                    "event_id": item.id,
                    "correlation_id": item.correlation_id,
                    "span": item.span_name,
                    "status": item.status,
                    "duration_ms": item.duration_ms,
                    "attributes": item.attributes,
                    "created_at": item.created_at.isoformat(),
                }
                for item in technical_events
            ],
        }

    def _decide(
        self,
        session_id: str,
        sanitized: str,
        raw: str,
        state: SessionState,
        collected: ExtractedData,
    ) -> AgentDecision:
        deterministic = deterministic_extract(raw)
        deterministic_intent_value = deterministic_intent(sanitized)
        metadata = self.provider.telemetry_metadata()
        policy_decision = self.policy.check(
            PolicyAction.CALL_LLM,
            session_id,
            {
                "sessionState": state.value,
                "sanitized": True,
                "destination": str(metadata["provider"]),
            },
        )
        if self.policy.blocks(PolicyAction.CALL_LLM, policy_decision):
            with self.telemetry.span(
                "deterministic_fallback",
                {
                    "session_id": session_id,
                    "state": state.value,
                    "error_category": "policy_denied",
                },
            ):
                return AgentDecision(
                    intent=deterministic_intent_value,
                    extracted=deterministic,
                    confidence=0.7,
                )
        with self.telemetry.span(
            "llm_decision",
            {
                **metadata,
                "session_id": session_id,
                "state": state.value,
                "prompt_version": PROMPT_VERSION,
                "schema_version": SCHEMA_VERSION,
                "cache_status": "disabled",
            },
        ) as decision_span:
            try:
                model_decision = self.provider.generate_decision(
                    message=sanitized, state=state, collected=collected
                )
                for key, value in self.provider.telemetry_metadata().items():
                    decision_span.set_attribute(key, value)
                decision_span.set_attribute("status", "success")
                extracted = model_decision.extracted.merge(deterministic)
                intent = (
                    deterministic_intent_value
                    if deterministic_intent_value is not Intent.UNKNOWN
                    else model_decision.intent
                )
                return AgentDecision(
                    intent=intent,
                    extracted=extracted,
                    confidence=model_decision.confidence,
                )
            except Exception as exc:
                decision_span.set_attribute("status", "fallback")
                decision_span.set_attribute("error_category", type(exc).__name__)
                logger.warning("provider_fallback", extra={"error_type": type(exc).__name__})
                with self.telemetry.span(
                    "deterministic_fallback",
                    {
                        "session_id": session_id,
                        "state": state.value,
                        "error_category": type(exc).__name__,
                    },
                ):
                    return AgentDecision(
                        intent=deterministic_intent_value,
                        extracted=deterministic,
                        confidence=0.7,
                    )

    def _run_quote(self, session_id: str, collected: ExtractedData) -> TurnResult:
        current = self.repository.get_session(session_id)
        policy_decision = self.policy.check(
            PolicyAction.CALL_QUOTE,
            session_id,
            {
                "sessionState": current.state,
                "dataComplete": not _missing_fields(collected),
                "confirmed": True,
                "sanitized": True,
                "destination": "quote-service",
            },
        )
        if self.policy.blocks(PolicyAction.CALL_QUOTE, policy_decision):
            return self._handoff(
                session_id,
                "policy_call_quote_denied",
                collected,
                "A política de autorização não permitiu a cotação automática. "
                "Vou encaminhar para atendimento humano sem estimar preço.",
            )
        self._update_session(session_id, state=SessionState.QUOTING, event="quote_requested")
        try:
            result = self.quote_client.quote(collected)
            self._record_quote_attempts(session_id, result.quote_id, result.attempts)
            self._update_session(
                session_id,
                state=SessionState.QUOTE_PRESENTED,
                quote_payload=result.payload,
                event="quote_succeeded",
            )
            return self._reply(session_id, _format_quote(result.payload))
        except QuoteRejected as exc:
            self._record_quote_attempts(session_id, exc.quote_id, exc.attempts)
            return self._handoff(
                session_id,
                "eligibility_rejected",
                collected,
                f"A cotação automática foi recusada: {exc.reason} "
                "Vou encaminhar para análise humana.",
            )
        except QuoteInvalid as exc:
            self._record_quote_attempts(session_id, exc.quote_id, exc.attempts)
            return self._handoff(
                session_id,
                "invalid_quote_payload",
                collected,
                "O serviço rejeitou os dados da cotação. Vou encaminhar para revisão humana.",
            )
        except QuoteUnavailable as exc:
            self._record_quote_attempts(session_id, exc.quote_id, exc.attempts)
            return self._handoff(
                session_id,
                "quote_service_unavailable",
                collected,
                "O serviço de cotação está temporariamente indisponível. "
                "Não vou estimar um preço; um atendente continuará o atendimento.",
            )

    def _complete(self, session_id: str) -> TurnResult:
        record = self.repository.get_session(session_id)
        policy_decision = self.policy.check(
            PolicyAction.COMPLETE_SESSION,
            session_id,
            {
                "sessionState": record.state,
                "quoteSucceeded": record.quote_payload is not None,
            },
        )
        if self.policy.blocks(PolicyAction.COMPLETE_SESSION, policy_decision):
            return self._handoff(
                session_id,
                "policy_complete_denied",
                ExtractedData.model_validate(record.collected),
                "A conclusão automática não foi autorizada. Vou encaminhar para revisão humana.",
            )
        with self.telemetry.span("completion", {"session_id": session_id}):
            self._update_session(
                session_id,
                state=SessionState.COMPLETED,
                status=SessionStatus.COMPLETED,
                event="lead_accepted_quote",
            )
        return self._reply(
            session_id,
            "Registrei seu interesse. A contratação e a emissão da apólice "
            "dependem da validação de um atendente.",
        )

    def _handoff(
        self,
        session_id: str,
        reason: str,
        collected: ExtractedData,
        message: str,
    ) -> TurnResult:
        summary = {
            "reason": reason,
            "collected": collected.model_dump(mode="json", exclude_none=True),
        }
        self.policy.check(
            PolicyAction.HANDOFF_SESSION,
            session_id,
            {"reasonPresent": bool(reason)},
        )
        with self.telemetry.span("handoff", {"session_id": session_id, "reason": reason}):
            self.repository.add_handoff(session_id, reason, summary)
            self._update_session(
                session_id,
                state=SessionState.HANDOFF,
                status=SessionStatus.HANDOFF,
                event=reason,
            )
        return self._reply(session_id, message)

    def _reply(self, session_id: str, content: str) -> TurnResult:
        message = self.repository.add_message(session_id, "assistant", "text", redact_pii(content))
        return TurnResult(
            session=self.get_session(session_id), assistant_message=_message_view(message)
        )

    def _update_session(
        self,
        session_id: str,
        *,
        state: SessionState | None = None,
        status: SessionStatus | None = None,
        collected: ExtractedData | None = None,
        clarification_count: int | None = None,
        quote_payload: dict[str, Any] | None = None,
        event: str | None = None,
    ) -> None:
        with self.telemetry.span(
            "state_transition",
            {
                "session_id": session_id,
                "target_state": state.value if state else "unchanged",
                "event": event or "data_update",
            },
        ):
            self.repository.update_session(
                session_id,
                state=state,
                status=status,
                collected=collected,
                clarification_count=clarification_count,
                quote_payload=quote_payload,
                event=event,
            )

    def _record_quote_attempts(
        self, session_id: str, quote_id: str, attempts: list[QuoteAttempt]
    ) -> None:
        for attempt in attempts:
            with self.telemetry.span(
                "quote_attempt",
                {
                    "session_id": session_id,
                    "quote_id": quote_id,
                    "quote_id_scope": "local_operation",
                    "attempt": attempt.attempt_no,
                    "status": attempt.status,
                    "http_status": attempt.http_status,
                    "duration_ms": attempt.duration_ms,
                    "error_category": attempt.error_code,
                },
            ):
                pass
        self.repository.add_quote_attempts(session_id, quote_id, attempts)


def _message_view(record: Any) -> MessageView:
    return MessageView(
        id=record.id,
        role=record.role,
        message_type=record.message_type,
        content=record.content,
        created_at=record.created_at,
    )


def _missing_fields(data: ExtractedData) -> list[str]:
    fields = [
        ("modelo do veículo", data.vehicle_model),
        ("ano do veículo", data.vehicle_year),
        ("idade", data.age),
        ("CEP", data.cep_prefix),
        ("plano", data.plan_id),
    ]
    return [name for name, value in fields if value is None]


def _question_for(field: str) -> str:
    questions = {
        "modelo do veículo": "Qual é o modelo do veículo?",
        "ano do veículo": "Qual é o ano do veículo?",
        "idade": "Qual é a sua idade?",
        "CEP": "Qual é o CEP onde o veículo pernoita? Usarei somente os dois primeiros dígitos.",
        "plano": (
            "Qual plano você quer cotar: Essencial, Completo ou Premium? "
            "Essencial cobre colisão, roubo e furto; Completo inclui terceiros e vidros; "
            "Premium acrescenta carro reserva e assistência 24h."
        ),
    }
    return questions[field]


def _confirmation_message(data: ExtractedData) -> str:
    return (
        f"Confirma a cotação do plano {data.plan_id} para {data.vehicle_model} "
        f"{data.vehicle_year}, condutor de {data.age} anos e região de CEP "
        f"{data.cep_prefix}***-***?"
    )


def _format_quote(payload: dict[str, Any]) -> str:
    price = float(payload["premio_mensal"])
    franchise = float(payload["franquia"])
    coverages = ", ".join(str(item).replace("_", " ") for item in payload["coberturas"])
    message = (
        f"Cotação oficial: plano {payload['plano_nome']}, mensalidade de R$ {_brl(price)}, "
        f"franquia de R$ {_brl(franchise)}. Coberturas: {coverages}. "
        f"Roubo e furto têm carência de {payload['carencia']['dias']} dias."
    )
    pro_rata = payload.get("primeiro_pagamento_pro_rata")
    if pro_rata:
        first_payment = float(pro_rata["valor_primeiro_pagamento"])
        message += f" Primeiro pagamento proporcional: R$ {_brl(first_payment)}."
    return message + " Deseja registrar interesse nesta proposta?"


def _brl(value: float) -> str:
    return f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
