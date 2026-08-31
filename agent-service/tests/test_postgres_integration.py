from __future__ import annotations

import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from autoseguro.api import create_app
from autoseguro.config import Settings
from autoseguro.models import MessageRecord, TraceEventRecord
from autoseguro.providers import FakeProvider
from autoseguro.quote_client import QuoteClient
from autoseguro.repository import Repository, create_db_engine


@pytest.mark.integration
def test_api_persists_sanitized_message_in_postgres() -> None:
    database_url = os.getenv("TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("TEST_DATABASE_URL não configurada")

    repository = Repository(create_db_engine(database_url))
    repository.create_schema()
    quote_client = QuoteClient(
        "http://quote.test",
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"status": "ok"})),
    )
    app = create_app(
        settings=Settings(database_url=database_url),
        repository=repository,
        provider=FakeProvider(),
        quote_client=quote_client,
    )
    client = TestClient(app)
    created = client.post("/v1/sessions").json()
    session_id = created["session"]["id"]
    headers = {
        "X-Session-Token": created["session_token"],
        "Idempotency-Key": "postgres-message-001",
    }
    response = client.post(
        f"/v1/sessions/{session_id}/messages",
        headers=headers,
        json={
            "content": "Corolla 2022, CPF 389.083.863-43, email teste@example.com",
            "message_type": "text",
        },
    )
    assert response.status_code == 200

    with repository.session_factory() as db:
        messages = list(
            db.scalars(select(MessageRecord).where(MessageRecord.session_id == session_id))
        )
        trace_events = list(
            db.scalars(select(TraceEventRecord).where(TraceEventRecord.session_id == session_id))
        )
    stored = " ".join(message.content for message in messages)
    assert "389.083.863-43" not in stored
    assert "teste@example.com" not in stored
    serialized_trace = json.dumps(
        [event.attributes for event in trace_events], ensure_ascii=False, sort_keys=True
    )
    assert trace_events
    assert "389.083.863-43" not in serialized_trace
    assert "teste@example.com" not in serialized_trace
    assert created["session_token"] not in serialized_trace
