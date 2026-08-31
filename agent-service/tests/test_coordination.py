from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from autoseguro.api import create_app
from autoseguro.config import Settings
from autoseguro.coordination import (
    CoordinationUnavailable,
    LocalCoordination,
    RedisCoordination,
    opaque_key,
)
from autoseguro.domain import ExtractedData
from autoseguro.providers import FakeProvider
from autoseguro.quote_client import QuoteClient, QuoteUnavailable
from autoseguro.repository import Repository, create_db_engine


def _quote_payload() -> dict[str, object]:
    return {
        "plano_id": "completo",
        "plano_nome": "Completo",
        "premio_mensal": 209.9,
        "franquia": 3000,
        "coberturas": ["colisao", "roubo"],
        "multiplicadores": {"faixa_etaria": 1.0, "idade_veiculo": 1.0, "regiao": 1.0},
        "carencia": {"coberturas": ["roubo"], "dias": 30, "observacao": "teste"},
        "moeda": "BRL",
    }


def _file_repository(tmp_path: Path) -> Repository:
    repository = Repository(create_db_engine(f"sqlite+pysqlite:///{tmp_path / 'concurrent.db'}"))
    repository.create_schema()
    return repository


def _headers(token: str, key: str) -> dict[str, str]:
    return {"X-Session-Token": token, "Idempotency-Key": key}


def test_repeated_concurrent_message_returns_one_quote_and_same_response(tmp_path: Path) -> None:
    repository = _file_repository(tmp_path)
    coordination = LocalCoordination()
    quote_calls = 0
    mutex = threading.Lock()

    def quote(_: httpx.Request) -> httpx.Response:
        nonlocal quote_calls
        with mutex:
            quote_calls += 1
        time.sleep(0.08)
        return httpx.Response(200, json=_quote_payload())

    quote_client = QuoteClient(
        "http://quote.test",
        transport=httpx.MockTransport(quote),
        sleeper=lambda _: None,
        coordination=coordination,
    )
    app = create_app(
        settings=Settings(database_url="sqlite+pysqlite:///unused.db"),
        repository=repository,
        provider=FakeProvider(),
        quote_client=quote_client,
        coordination=coordination,
    )
    client = TestClient(app)
    created = client.post("/v1/sessions").json()
    session_id = created["session"]["id"]
    token = created["session_token"]
    messages = [
        ("Meu veículo é um Toyota Corolla 2022", "prepare-001"),
        ("Tenho 35 anos e CEP 01310-100", "prepare-002"),
        ("Quero o plano completo", "prepare-003"),
    ]
    for content, key in messages:
        response = client.post(
            f"/v1/sessions/{session_id}/messages",
            headers=_headers(token, key),
            json={"content": content, "message_type": "text"},
        )
        assert response.status_code == 200

    def confirm() -> httpx.Response:
        return client.post(
            f"/v1/sessions/{session_id}/messages",
            headers=_headers(token, "confirm-retry-001"),
            json={"content": "confirmo", "message_type": "text"},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: confirm(), range(2)))

    assert [response.status_code for response in responses] == [200, 200]
    assert responses[0].json() == responses[1].json()
    assert quote_calls == 1
    record = repository.get_session(session_id)
    assert len(record.quote_attempts) == 1
    assert sum(item.to_state == "quote_presented" for item in record.transitions) == 1
    assert sum(item.content == "confirmo" for item in record.messages) == 1


def test_idempotency_key_reuse_with_different_payload_is_rejected(
    repository: Repository, quote_client: QuoteClient
) -> None:
    coordination = LocalCoordination()
    client = TestClient(
        create_app(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            repository=repository,
            provider=FakeProvider(),
            quote_client=quote_client,
            coordination=coordination,
        )
    )
    created = client.post("/v1/sessions").json()
    session_id = created["session"]["id"]
    headers = _headers(created["session_token"], "same-key-001")
    first = client.post(
        f"/v1/sessions/{session_id}/messages",
        headers=headers,
        json={"content": "Toyota Corolla 2022", "message_type": "text"},
    )
    second = client.post(
        f"/v1/sessions/{session_id}/messages",
        headers=headers,
        json={"content": "Honda Civic 2021", "message_type": "text"},
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == "IDEMPOTENCY_CONFLICT"


class _UnavailableCoordination(LocalCoordination):
    name = "redis"

    def health(self) -> bool:
        return False

    def rate_limit(self, key: str, *, limit: int, window_seconds: int):  # type: ignore[no-untyped-def]
        raise CoordinationUnavailable("redis_down")

    def get_idempotency(self, key: str) -> str | None:
        raise CoordinationUnavailable("redis_down")


def test_redis_unavailable_degrades_rate_limit_but_blocks_message(
    repository: Repository, quote_client: QuoteClient
) -> None:
    coordination = _UnavailableCoordination()
    app = create_app(
        settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
        repository=repository,
        provider=FakeProvider(),
        quote_client=quote_client,
        coordination=coordination,
    )
    client = TestClient(app)
    created = client.post("/v1/sessions")
    assert created.status_code == 201
    assert created.headers["x-control-degraded"] == "rate_limit_local"
    payload = created.json()

    response = client.post(
        f"/v1/sessions/{payload['session']['id']}/messages",
        headers=_headers(payload["session_token"], "redis-down-001"),
        json={"content": "Toyota Corolla 2022", "message_type": "text"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "COORDINATION_UNAVAILABLE"
    record = repository.get_session(payload["session"]["id"])
    assert [item.role for item in record.messages] == ["assistant"]


def test_idempotency_storage_contains_no_raw_pii_or_capability_token(
    repository: Repository, quote_client: QuoteClient
) -> None:
    coordination = LocalCoordination()
    client = TestClient(
        create_app(
            settings=Settings(database_url="sqlite+pysqlite:///:memory:"),
            repository=repository,
            provider=FakeProvider(),
            quote_client=quote_client,
            coordination=coordination,
        )
    )
    created = client.post("/v1/sessions").json()
    session_id = created["session"]["id"]
    token = created["session_token"]
    key = "pii-storage-001"
    response = client.post(
        f"/v1/sessions/{session_id}/messages",
        headers=_headers(token, key),
        json={
            "content": "Corolla 2022, CPF 389.083.863-43, email teste@example.com",
            "message_type": "text",
        },
    )
    assert response.status_code == 200
    stored = coordination.get_idempotency(opaque_key("idempotency", session_id, key))
    assert stored is not None
    assert "389.083.863-43" not in stored
    assert "teste@example.com" not in stored
    assert token not in stored


def test_shared_circuit_breaker_opens_after_deterministic_failures() -> None:
    coordination = LocalCoordination()
    calls = 0

    def unavailable(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"error": "unavailable"})

    client = QuoteClient(
        "http://quote.test",
        max_attempts=1,
        transport=httpx.MockTransport(unavailable),
        sleeper=lambda _: None,
        coordination=coordination,
        circuit_breaker_threshold=2,
        circuit_breaker_open_seconds=30,
    )
    data = ExtractedData(
        age=35,
        vehicle_model="Corolla",
        vehicle_year=2022,
        cep_prefix="01",
        plan_id="completo",
    )
    for _ in range(2):
        with pytest.raises(QuoteUnavailable):
            client.quote(data)
    with pytest.raises(QuoteUnavailable) as raised:
        client.quote(data)

    assert calls == 2
    assert raised.value.attempts[0].status == "circuit_open"


@pytest.mark.integration
def test_real_redis_coordination_and_opaque_storage() -> None:
    redis_url = os.getenv("TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("TEST_REDIS_URL não configurada")

    coordination = RedisCoordination.from_url(redis_url)
    suffix = str(time.time_ns())
    rate_key = opaque_key("rate-test", suffix)
    idem_key = opaque_key("idempotency-test", suffix)
    lock_key = opaque_key("lock-test", suffix)
    assert coordination.health()
    assert coordination.rate_limit(rate_key, limit=1, window_seconds=10).allowed
    assert not coordination.rate_limit(rate_key, limit=1, window_seconds=10).allowed
    coordination.put_idempotency(idem_key, '{"content":"[EMAIL_REDACTED]"}', ttl_seconds=60)
    assert coordination.get_idempotency(idem_key) == '{"content":"[EMAIL_REDACTED]"}'
    lease = coordination.acquire_lock(lock_key, ttl_seconds=10)
    assert lease is not None
    assert coordination.acquire_lock(lock_key, ttl_seconds=10) is None
    lease.release()


def test_redis_coordination_applies_run_namespace() -> None:
    client = pytest.importorskip("fakeredis").FakeRedis(decode_responses=True)
    coordination = RedisCoordination(client, key_prefix="autoseguro:validation:run-123")
    key = opaque_key("idempotency", "session", "request")
    coordination.put_idempotency(key, "safe", ttl_seconds=60)
    assert client.get(f"autoseguro:validation:run-123:{key}") == "safe"
    assert client.get(key) is None
