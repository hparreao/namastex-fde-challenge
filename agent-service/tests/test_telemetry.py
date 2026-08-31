from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from autoseguro.api import create_app
from autoseguro.config import Settings
from autoseguro.providers import FakeProvider, LLMProvider
from autoseguro.quote_client import QuoteClient
from autoseguro.repository import Repository
from autoseguro.telemetry import (
    InMemoryTelemetryBackend,
    OpenTelemetryBackend,
    SafeTelemetry,
    SpanHandle,
)


def _client(
    repository: Repository,
    quote_client: QuoteClient,
    telemetry: SafeTelemetry,
    provider: LLMProvider | None = None,
) -> TestClient:
    return TestClient(
        create_app(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            repository=repository,
            provider=provider or FakeProvider(),
            quote_client=quote_client,
            telemetry=telemetry,
        )
    )


def test_trace_links_message_flow_without_pii_or_tokens(
    repository: Repository, quote_client: QuoteClient
) -> None:
    backend = InMemoryTelemetryBackend()
    client = _client(repository, quote_client, SafeTelemetry(backend))
    created = client.post("/v1/sessions").json()
    token = created["session_token"]
    response = client.post(
        f"/v1/sessions/{created['session']['id']}/messages",
        headers={"X-Session-Token": token, "Idempotency-Key": "telemetry-001"},
        json={
            "content": "Corolla 2022, CPF 389.083.863-43, email teste@example.com",
            "message_type": "text",
        },
    )
    assert response.status_code == 200
    assert re.fullmatch(r"[0-9a-f-]{36}", response.headers["x-message-id"])

    names = {record.name for record in backend.records}
    assert {
        "message",
        "authorization",
        "idempotency",
        "session_lock",
        "pii_redaction",
        "llm_decision",
        "state_transition",
    }.issubset(names)
    exported = json.dumps(
        [
            {
                "name": record.name,
                "attributes": record.attributes,
                "events": record.events,
            }
            for record in backend.records
        ],
        ensure_ascii=False,
    )
    assert "389.083.863-43" not in exported
    assert "teste@example.com" not in exported
    assert token not in exported
    assert "Corolla 2022" not in exported
    llm = next(record for record in backend.records if record.name == "llm_decision")
    assert llm.attributes["message_id"] == response.headers["x-message-id"]
    assert llm.attributes["provider"] == "fake"
    assert llm.attributes["model"] == "deterministic-v1"
    assert llm.attributes["prompt_version"] == "2026-08-28.1"
    assert llm.attributes["schema_version"] == "agent-decision.v1"


def test_trace_reaches_quote_attempt_and_completion(
    repository: Repository, quote_client: QuoteClient
) -> None:
    backend = InMemoryTelemetryBackend()
    client = _client(repository, quote_client, SafeTelemetry(backend))
    created = client.post("/v1/sessions").json()
    session_id = created["session"]["id"]
    token = created["session_token"]
    messages = [
        "Toyota Corolla 2022",
        "Tenho 35 anos e CEP 01310-100",
        "Quero o plano completo",
        "confirmo",
        "fechado",
    ]
    for index, content in enumerate(messages):
        response = client.post(
            f"/v1/sessions/{session_id}/messages",
            headers={
                "X-Session-Token": token,
                "Idempotency-Key": f"trace-flow-{index:03d}",
                "X-Correlation-ID": f"trace-correlation-{index:03d}",
            },
            json={"content": content, "message_type": "text"},
        )
        assert response.status_code == 200

    names = [record.name for record in backend.records]
    assert "quote_attempt" in names
    assert "completion" in names
    quote = next(record for record in backend.records if record.name == "quote_attempt")
    assert quote.attributes["status"] == "success"
    assert quote.attributes["http_status"] == 200
    roots = [record for record in backend.records if record.name == "message"]
    assert len(roots) == 5
    assert roots[-1].attributes["correlation_id"] == "trace-correlation-004"
    assert re.fullmatch(r"trace_[0-9a-f]{32}", str(roots[-1].attributes["canonical_trace_id"]))
    persisted = client.get(
        f"/v1/sessions/{session_id}/trace",
        headers={"X-Session-Token": token},
    )
    assert persisted.status_code == 200
    technical = persisted.json()["technical_events"]
    assert all("event_id" in item for item in technical)
    persisted_names = {item["span"] for item in technical}
    assert {"authorization", "llm_decision", "cedar_policy_decision", "quote_attempt"}.issubset(
        persisted_names
    )
    assert any(item["correlation_id"] == "trace-correlation-004" for item in technical)
    assert all(
        re.fullmatch(r"trace_[0-9a-f]{32}", item["attributes"]["canonical_trace_id"])
        for item in technical
        if item["correlation_id"] is not None
    )
    serialized = json.dumps(technical, ensure_ascii=False)
    assert token not in serialized
    assert "01310-100" not in serialized


class _FailingBackend:
    @contextmanager
    def span(
        self, name: str, attributes: Mapping[str, str | int | float | bool]
    ) -> Iterator[SpanHandle]:
        del name, attributes
        raise ConnectionError("collector unavailable")
        yield  # pragma: no cover


def test_observability_unavailable_never_interrupts_application(
    repository: Repository, quote_client: QuoteClient
) -> None:
    client = _client(repository, quote_client, SafeTelemetry(_FailingBackend()))
    created = client.post("/v1/sessions").json()
    response = client.post(
        f"/v1/sessions/{created['session']['id']}/messages",
        headers={
            "X-Session-Token": created["session_token"],
            "Idempotency-Key": "telemetry-down-001",
        },
        json={"content": "Toyota Corolla 2022", "message_type": "text"},
    )
    assert response.status_code == 200


class _BrokenProvider(FakeProvider):
    provider_name = "broken"
    model = "broken-v1"

    def generate_decision(self, **kwargs):  # type: ignore[no-untyped-def]
        del kwargs
        raise TimeoutError("provider timeout")


def test_deterministic_fallback_is_traced_without_error_details(
    repository: Repository, quote_client: QuoteClient
) -> None:
    backend = InMemoryTelemetryBackend()
    client = _client(
        repository,
        quote_client,
        SafeTelemetry(backend),
        provider=_BrokenProvider(),
    )
    created = client.post("/v1/sessions").json()
    response = client.post(
        f"/v1/sessions/{created['session']['id']}/messages",
        headers={
            "X-Session-Token": created["session_token"],
            "Idempotency-Key": "fallback-001",
        },
        json={"content": "Toyota Corolla 2022", "message_type": "text"},
    )
    assert response.status_code == 200
    names = [record.name for record in backend.records]
    assert "deterministic_fallback" in names
    llm = next(record for record in backend.records if record.name == "llm_decision")
    assert llm.attributes["status"] == "fallback"
    assert llm.attributes["error_category"] == "TimeoutError"


def test_real_otlp_export_masks_pii_and_forbidden_attributes() -> None:
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
        ExportTraceServiceRequest,
    )

    received: list[bytes] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers["Content-Length"])
            received.append(self.rfile.read(length))
            self.send_response(200)
            self.send_header("Content-Type", "application/x-protobuf")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    try:
        server = HTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("sandbox não permite abrir listener local")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    backend = OpenTelemetryBackend(
        f"http://127.0.0.1:{server.server_port}/v1/traces", "", "test-service"
    )
    telemetry = SafeTelemetry(backend)
    try:
        with telemetry.span(
            "safe-span",
            {
                "customer_email": "teste@example.com",
                "session_token": "capability-token-must-not-export",
                "raw_content": "Corolla 2022",
                "provider": "fake",
            },
        ):
            pass
        assert backend.provider.force_flush(timeout_millis=2000)
    finally:
        backend.provider.shutdown()
        server.shutdown()
        thread.join(timeout=2)

    assert received
    request = ExportTraceServiceRequest.FromString(received[0])
    spans = [
        span
        for resource in request.resource_spans
        for scope in resource.scope_spans
        for span in scope.spans
    ]
    attributes = {attribute.key: attribute.value for attribute in spans[0].attributes}
    serialized = str(attributes)
    assert "teste@example.com" not in serialized
    assert "capability-token-must-not-export" not in serialized
    assert "Corolla 2022" not in serialized
    assert "[EMAIL_REDACTED]" in serialized


def test_real_otlp_exporter_unreachable_does_not_raise() -> None:
    backend = OpenTelemetryBackend("http://127.0.0.1:1/v1/traces", "", "test-service-unreachable")
    telemetry = SafeTelemetry(backend)
    with telemetry.span("message", {"status": "ok"}):
        pass
    backend.provider.force_flush(timeout_millis=1500)
    backend.provider.shutdown()
