from __future__ import annotations

import httpx
import pytest
from conftest import quote_payload

from autoseguro.domain import ExtractedData
from autoseguro.quote_client import QuoteClient, QuoteInvalid, QuoteRejected, QuoteUnavailable

DATA = ExtractedData(
    age=35, vehicle_model="Corolla", vehicle_year=2022, cep_prefix="01", plan_id="completo"
)


def test_retries_transient_failure_then_succeeds() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(503, json={"error": "upstream_unavailable"})
        return httpx.Response(200, json=quote_payload())

    client = QuoteClient(
        "http://quote.test",
        transport=httpx.MockTransport(handler),
        sleeper=lambda _: None,
        jitter=lambda: 0,
    )
    result = client.quote(DATA)
    assert calls == 3
    assert [attempt.status for attempt in result.attempts] == [
        "retryable_error",
        "retryable_error",
        "success",
    ]


def test_exhaustion_never_returns_price() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(500, json={"error": "upstream_unavailable"})
    )
    client = QuoteClient(
        "http://quote.test", transport=transport, sleeper=lambda _: None, jitter=lambda: 0
    )
    with pytest.raises(QuoteUnavailable) as error:
        client.quote(DATA)
    assert len(error.value.attempts) == 3


@pytest.mark.parametrize("status", [500, 502, 503])
def test_retryable_statuses_exhaust_without_price(status: int) -> None:
    client = QuoteClient(
        "http://quote.test",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json={"error": "upstream_unavailable"})
        ),
        sleeper=lambda _: None,
        jitter=lambda: 0,
    )
    with pytest.raises(QuoteUnavailable) as error:
        client.quote(DATA)
    assert [attempt.http_status for attempt in error.value.attempts] == [status] * 3


def test_http_400_is_not_retried() -> None:
    calls = 0

    def invalid(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": "payload_invalido"})

    client = QuoteClient("http://quote.test", transport=httpx.MockTransport(invalid))
    with pytest.raises(QuoteInvalid):
        client.quote(DATA)
    assert calls == 1


def test_connection_refused_is_retried_and_exhausted() -> None:
    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = QuoteClient(
        "http://quote.test",
        transport=httpx.MockTransport(refused),
        sleeper=lambda _: None,
        jitter=lambda: 0,
    )
    with pytest.raises(QuoteUnavailable) as error:
        client.quote(DATA)
    assert [attempt.status for attempt in error.value.attempts] == ["transport_error"] * 3


def test_business_rejection_is_not_retried() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(422, json={"error": "cotacao_recusada", "motivo": "idade"})
    )
    client = QuoteClient("http://quote.test", transport=transport, sleeper=lambda _: None)
    with pytest.raises(QuoteRejected) as error:
        client.quote(DATA)
    assert error.value.reason == "idade"
    assert len(error.value.attempts) == 1


def test_timeouts_are_retried_and_exhausted() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("legacy demorou 8 segundos", request=request)

    client = QuoteClient(
        "http://quote.test",
        transport=httpx.MockTransport(timeout),
        sleeper=lambda _: None,
        jitter=lambda: 0,
    )
    with pytest.raises(QuoteUnavailable) as error:
        client.quote(DATA)
    assert [attempt.status for attempt in error.value.attempts] == ["timeout"] * 3


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json={"premio_mensal": 123.45}),
    ],
)
def test_invalid_success_response_is_rejected_without_price(response: httpx.Response) -> None:
    client = QuoteClient(
        "http://quote.test",
        transport=httpx.MockTransport(lambda _: response),
        sleeper=lambda _: None,
    )
    with pytest.raises(QuoteInvalid) as error:
        client.quote(DATA)
    assert len(error.value.attempts) == 1
    assert error.value.attempts[0].http_status == 200
    assert error.value.attempts[0].error_code == "invalid_response_schema"
