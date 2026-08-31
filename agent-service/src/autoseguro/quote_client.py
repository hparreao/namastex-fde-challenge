from __future__ import annotations

import logging
import random
import time
import uuid
from collections.abc import Callable
from typing import Any

import httpx
from pydantic import ValidationError

from .coordination import CoordinationBackend, CoordinationUnavailable, opaque_key
from .domain import ExtractedData, QuoteAttempt, QuotePayload, QuoteResult

logger = logging.getLogger(__name__)


class QuoteError(Exception):
    def __init__(self, message: str, quote_id: str, attempts: list[QuoteAttempt]) -> None:
        super().__init__(message)
        self.quote_id = quote_id
        self.attempts = attempts


class QuoteUnavailable(QuoteError):
    pass


class QuoteRejected(QuoteError):
    def __init__(self, reason: str, quote_id: str, attempts: list[QuoteAttempt]) -> None:
        super().__init__(reason, quote_id, attempts)
        self.reason = reason


class QuoteInvalid(QuoteError):
    pass


class QuoteClient:
    RETRYABLE = {500, 502, 503}

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 3.0,
        max_attempts: int = 3,
        backoff_seconds: float = 0.25,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        coordination: CoordinationBackend | None = None,
        circuit_breaker_threshold: int = 3,
        circuit_breaker_open_seconds: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.transport = transport
        self.sleeper = sleeper
        self.jitter = jitter
        self.coordination = coordination
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_open_seconds = circuit_breaker_open_seconds
        self.circuit_key = opaque_key("circuit", self.base_url, "quote")

    def health(self) -> bool:
        try:
            with httpx.Client(transport=self.transport, timeout=self.timeout_seconds) as client:
                return client.get(f"{self.base_url}/health").status_code == 200
        except httpx.HTTPError:
            return False

    def quote(self, data: ExtractedData) -> QuoteResult:
        quote_id = str(uuid.uuid4())
        attempts: list[QuoteAttempt] = []
        if self._circuit_is_open():
            attempts.append(
                QuoteAttempt(
                    attempt_no=0,
                    status="circuit_open",
                    duration_ms=0,
                    error_code="circuit_open",
                )
            )
            raise QuoteUnavailable("Circuit breaker aberto.", quote_id, attempts)
        payload: dict[str, Any] = {
            "plano_id": data.plan_id,
            "idade": data.age,
            "veiculo_ano": data.vehicle_year,
            "cep": data.cep_prefix,
            "data_inicio": data.start_date.isoformat() if data.start_date else None,
        }

        with httpx.Client(transport=self.transport, timeout=self.timeout_seconds) as client:
            for attempt_no in range(1, self.max_attempts + 1):
                started = time.monotonic()
                try:
                    response = client.post(f"{self.base_url}/quote", json=payload)
                    duration_ms = round((time.monotonic() - started) * 1000)
                    body = _safe_json(response)
                    if response.status_code == 200:
                        try:
                            body = QuotePayload.model_validate(body).model_dump(mode="json")
                        except ValidationError as exc:
                            attempts.append(
                                QuoteAttempt(
                                    attempt_no=attempt_no,
                                    status="invalid",
                                    http_status=200,
                                    duration_ms=duration_ms,
                                    error_code="invalid_response_schema",
                                )
                            )
                            raise QuoteInvalid(
                                "Quote-service retornou resposta inválida.", quote_id, attempts
                            ) from exc
                        attempts.append(
                            QuoteAttempt(
                                attempt_no=attempt_no,
                                status="success",
                                http_status=200,
                                duration_ms=duration_ms,
                                response_payload=body,
                            )
                        )
                        self._circuit_success()
                        return QuoteResult(quote_id=quote_id, payload=body, attempts=attempts)
                    if response.status_code == 422:
                        attempts.append(
                            QuoteAttempt(
                                attempt_no=attempt_no,
                                status="rejected",
                                http_status=422,
                                duration_ms=duration_ms,
                                error_code="cotacao_recusada",
                            )
                        )
                        raise QuoteRejected(
                            str(body.get("motivo", "Cotação recusada.")), quote_id, attempts
                        )
                    if response.status_code not in self.RETRYABLE:
                        attempts.append(
                            QuoteAttempt(
                                attempt_no=attempt_no,
                                status="invalid",
                                http_status=response.status_code,
                                duration_ms=duration_ms,
                                error_code=str(body.get("error", "payload_invalido")),
                            )
                        )
                        raise QuoteInvalid("Quote-service recusou o payload.", quote_id, attempts)
                    attempts.append(
                        QuoteAttempt(
                            attempt_no=attempt_no,
                            status="retryable_error",
                            http_status=response.status_code,
                            duration_ms=duration_ms,
                            error_code=str(body.get("error", "upstream_unavailable")),
                        )
                    )
                except QuoteError:
                    raise
                except httpx.TimeoutException:
                    attempts.append(
                        QuoteAttempt(
                            attempt_no=attempt_no,
                            status="timeout",
                            duration_ms=round((time.monotonic() - started) * 1000),
                            error_code="timeout",
                        )
                    )
                except httpx.HTTPError:
                    attempts.append(
                        QuoteAttempt(
                            attempt_no=attempt_no,
                            status="transport_error",
                            duration_ms=round((time.monotonic() - started) * 1000),
                            error_code="transport_error",
                        )
                    )

                if attempt_no < self.max_attempts:
                    delay = self.backoff_seconds * (2 ** (attempt_no - 1))
                    self.sleeper(delay + self.jitter() * self.backoff_seconds)

        self._circuit_failure()
        raise QuoteUnavailable("Serviço de cotação indisponível.", quote_id, attempts)

    def _circuit_is_open(self) -> bool:
        if self.coordination is None:
            return False
        try:
            return self.coordination.circuit_is_open(self.circuit_key)
        except CoordinationUnavailable:
            logger.warning("circuit_breaker_degraded", extra={"operation": "read"})
            return False

    def _circuit_success(self) -> None:
        if self.coordination is None:
            return
        try:
            self.coordination.circuit_success(self.circuit_key)
        except CoordinationUnavailable:
            logger.warning("circuit_breaker_degraded", extra={"operation": "reset"})

    def _circuit_failure(self) -> None:
        if self.coordination is None:
            return
        try:
            self.coordination.circuit_failure(
                self.circuit_key,
                threshold=self.circuit_breaker_threshold,
                open_seconds=self.circuit_breaker_open_seconds,
            )
        except CoordinationUnavailable:
            logger.warning("circuit_breaker_degraded", extra={"operation": "write"})


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        parsed = response.json()
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except ValueError:
        return {"error": "invalid_json"}
