from __future__ import annotations

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from autoseguro.providers import FakeProvider
from autoseguro.quote_client import QuoteClient
from autoseguro.repository import Repository
from autoseguro.service import AgentService


def quote_payload() -> dict[str, object]:
    return {
        "plano_id": "completo",
        "plano_nome": "Completo",
        "premio_mensal": 209.9,
        "franquia": 3000,
        "coberturas": ["colisao", "roubo", "furto", "terceiros", "vidros"],
        "multiplicadores": {"faixa_etaria": 1.0, "idade_veiculo": 1.0, "regiao": 1.0},
        "carencia": {"coberturas": ["roubo", "furto"], "dias": 30, "observacao": "teste"},
        "moeda": "BRL",
    }


@pytest.fixture
def repository() -> Repository:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    repo = Repository(engine)
    repo.create_schema()
    return repo


@pytest.fixture
def quote_client() -> QuoteClient:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=quote_payload()))
    return QuoteClient("http://quote.test", transport=transport, sleeper=lambda _: None)


@pytest.fixture
def service(repository: Repository, quote_client: QuoteClient) -> AgentService:
    return AgentService(repository, FakeProvider(), quote_client)
