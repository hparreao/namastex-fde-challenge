from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from autoseguro.api import create_app
from autoseguro.config import Settings
from autoseguro.providers import FakeProvider
from autoseguro.quote_client import QuoteClient
from autoseguro.repository import Repository


def _app(
    repository: Repository,
    quote_client: QuoteClient,
    **settings: object,
):  # type: ignore[no-untyped-def]
    return create_app(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:", **settings),
        repository=repository,
        provider=FakeProvider(),
        quote_client=quote_client,
    )


def _create(client: TestClient) -> tuple[str, str]:
    response = client.post("/v1/sessions")
    assert response.status_code == 201
    payload = response.json()
    return payload["session"]["id"], payload["session_token"]


def test_session_api_requires_capability_token(
    repository: Repository, quote_client: QuoteClient
) -> None:
    client = TestClient(_app(repository, quote_client))
    session_id, token = _create(client)
    headers = {"X-Session-Token": token, "Idempotency-Key": "message-001"}

    response = client.post(
        f"/v1/sessions/{session_id}/messages",
        headers=headers,
        json={"content": "Toyota Corolla 2022", "message_type": "text"},
    )

    assert response.status_code == 200
    assert response.headers["x-correlation-id"]
    assert response.headers["x-message-id"]
    assert client.get(f"/v1/sessions/{session_id}", headers=headers).status_code == 200
    assert client.get(f"/v1/sessions/{session_id}/trace", headers=headers).status_code == 200


def test_session_id_cannot_authorize_another_session(
    repository: Repository, quote_client: QuoteClient
) -> None:
    client = TestClient(_app(repository, quote_client))
    first_id, first_token = _create(client)
    second_id, second_token = _create(client)

    assert first_id != second_id
    assert first_token != second_token
    response = client.get(f"/v1/sessions/{second_id}", headers={"X-Session-Token": first_token})
    assert response.status_code == 404
    assert response.json()["code"] == "SESSION_NOT_FOUND"
    assert client.get(f"/v1/sessions/{second_id}").status_code == 404


def test_session_token_is_returned_once_and_only_hash_is_stored(
    repository: Repository, quote_client: QuoteClient
) -> None:
    client = TestClient(_app(repository, quote_client))
    session_id, token = _create(client)

    record = repository.get_session(session_id)
    assert record.session_token_hash
    assert record.session_token_hash != token
    response = client.get(f"/v1/sessions/{session_id}", headers={"X-Session-Token": token})
    assert token not in response.text


def test_missing_session_returns_safe_404(
    repository: Repository, quote_client: QuoteClient
) -> None:
    response = TestClient(_app(repository, quote_client)).get(
        "/v1/sessions/missing", headers={"X-Session-Token": "invalid"}
    )
    assert response.status_code == 404
    assert response.json()["message"] == "Sessão não encontrada."
    assert "missing" not in response.text


def test_validation_errors_do_not_echo_sensitive_input(
    repository: Repository, quote_client: QuoteClient
) -> None:
    client = TestClient(_app(repository, quote_client))
    session_id, token = _create(client)
    sensitive = "teste@example.com" * 300
    response = client.post(
        f"/v1/sessions/{session_id}/messages",
        headers={"X-Session-Token": token, "Idempotency-Key": "validation-001"},
        json={"content": sensitive, "message_type": "text"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "teste@example.com" not in response.text


def test_rate_limit_has_retry_after_and_safe_headers(
    repository: Repository, quote_client: QuoteClient
) -> None:
    client = TestClient(
        _app(
            repository,
            quote_client,
            session_create_rate_limit=2,
            rate_limit_window_seconds=60,
        )
    )

    first = client.post("/v1/sessions")
    assert first.headers["x-content-type-options"] == "nosniff"
    assert first.headers["x-frame-options"] == "DENY"
    assert first.headers["cache-control"] == "no-store"
    assert "strict-transport-security" not in first.headers
    assert client.post("/v1/sessions").status_code == 201
    limited = client.post("/v1/sessions")
    assert limited.status_code == 429
    assert limited.json()["code"] == "RATE_LIMITED"
    assert int(limited.headers["retry-after"]) >= 1


def test_cors_uses_explicit_allowlist(repository: Repository, quote_client: QuoteClient) -> None:
    client = TestClient(
        _app(repository, quote_client, cors_allowed_origins="https://app.example.com")
    )
    preflight_headers = {
        "Origin": "https://app.example.com",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "X-Session-Token,Idempotency-Key,Content-Type",
    }
    allowed = client.options("/v1/sessions/example/messages", headers=preflight_headers)
    denied = client.options(
        "/v1/sessions/example/messages",
        headers={**preflight_headers, "Origin": "https://evil.example"},
    )
    assert allowed.headers["access-control-allow-origin"] == "https://app.example.com"
    assert "access-control-allow-origin" not in denied.headers


def test_database_errors_do_not_leak_details(
    repository: Repository, quote_client: QuoteClient
) -> None:
    app = _app(repository, quote_client)
    client = TestClient(app, raise_server_exceptions=False)
    session_id, token = _create(client)

    def fail(_: str):  # type: ignore[no-untyped-def]
        raise OperationalError("SELECT secret", {"password": "secret"}, RuntimeError("down"))

    app.state.service.get_session = fail
    response = client.get(f"/v1/sessions/{session_id}", headers={"X-Session-Token": token})
    assert response.status_code == 503
    assert response.json()["code"] == "DATABASE_UNAVAILABLE"
    assert "secret" not in response.text


def test_unexpected_errors_use_generic_envelope(
    repository: Repository, quote_client: QuoteClient
) -> None:
    app = _app(repository, quote_client)
    client = TestClient(app, raise_server_exceptions=False)
    session_id, token = _create(client)

    def fail(_: str):  # type: ignore[no-untyped-def]
        raise RuntimeError("sensitive internal detail")

    app.state.service.get_session = fail
    response = client.get(f"/v1/sessions/{session_id}", headers={"X-Session-Token": token})
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert "sensitive internal detail" not in response.text
