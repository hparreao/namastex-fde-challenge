from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from .config import Settings
from .coordination import coordination_from_config
from .policy import policy_from_config
from .providers import FakeProvider, provider_from_settings
from .quote_client import QuoteClient
from .repository import Repository, create_db_engine
from .service import AgentService, TurnResult
from .telemetry import telemetry_from_config, with_repository_telemetry

app = typer.Typer(no_args_is_help=True, help="CLI do agente AutoSeguro")


def _service(provider_name: str | None = None) -> AgentService:
    overrides: dict[str, Any] = {}
    if provider_name:
        overrides["llm_provider"] = provider_name
    settings = Settings(**overrides)
    repository = Repository(
        create_db_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_recycle=settings.db_pool_recycle_seconds,
        )
    )
    provider = (
        FakeProvider() if settings.llm_provider == "fake" else provider_from_settings(settings)
    )
    coordination = coordination_from_config(settings.coordination_backend, settings.redis_url)
    telemetry = with_repository_telemetry(
        repository,
        telemetry_from_config(
            enabled=settings.telemetry_enabled,
            endpoint=settings.otel_exporter_otlp_endpoint,
            headers=settings.otel_exporter_otlp_headers,
            service_name=settings.telemetry_service_name,
        ),
    )
    policy = policy_from_config(
        mode=settings.policy_mode,
        policy_path=settings.cedar_policy_path,
        schema_path=settings.cedar_schema_path,
        enforce_actions=settings.policy_enforce_actions,
        telemetry=telemetry,
    )
    quote_client = QuoteClient(
        settings.quote_service_url,
        timeout_seconds=settings.quote_timeout_seconds,
        max_attempts=settings.quote_max_attempts,
        backoff_seconds=settings.quote_backoff_seconds,
        coordination=coordination,
        circuit_breaker_threshold=settings.circuit_breaker_threshold,
        circuit_breaker_open_seconds=settings.circuit_breaker_open_seconds,
    )
    return AgentService(
        repository,
        provider,
        quote_client,
        max_messages_per_session=settings.max_messages_per_session,
        telemetry=telemetry,
        policy=policy,
    )


@app.command()
def chat(
    provider: str | None = typer.Option(None, help="openai, agents_sdk, anthropic ou fake"),
) -> None:
    """Inicia uma conversa interativa persistida."""
    service = _service(provider)
    result: TurnResult = service.create_session()
    typer.echo(result.assistant_message.content)
    while result.session.status.value == "active":
        message = typer.prompt("Você")
        result = service.handle_message(result.session.id, message)
        typer.echo(f"AutoSeguro: {result.assistant_message.content}")
    typer.echo(f"Sessão encerrada com status: {result.session.status.value}")


@app.command()
def demo(
    output: Annotated[Path, typer.Option(help="Arquivo JSONL de saída")] = Path(
        "../artifacts/demo-conversation.jsonl"
    ),
    provider: Annotated[str, typer.Option(help="openai, agents_sdk, anthropic ou fake")] = "fake",
) -> None:
    """Executa um caminho feliz reproduzível e exporta o histórico sanitizado."""
    service = _service(provider)
    result: TurnResult = service.create_session()
    session_id = result.session.id
    inputs = [
        "Meu veículo é um Toyota Corolla 2022",
        "Tenho 35 anos e o CEP é 01310-100",
        "Quero o plano completo",
        "confirmo",
        "fechado",
    ]
    for message in inputs:
        if result.session.status.value != "active":
            break
        result = service.handle_message(session_id, message)

    session = service.get_session(session_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
        for item in session.messages
    ]
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    trace_output = output.with_name(f"{output.stem}-trace.json")
    trace_output.write_text(
        json.dumps(service.trace(session_id), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    typer.echo(f"Sessão: {session_id}")
    typer.echo(f"Status: {session.status.value}")
    typer.echo(f"Log sanitizado: {output.resolve()}")
    typer.echo(f"Trace sanitizada: {trace_output.resolve()}")


@app.command("init-db")
def init_db() -> None:
    """Cria o schema diretamente; Alembic é preferível fora do desenvolvimento."""
    settings = Settings()
    repository = Repository(
        create_db_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            pool_recycle=settings.db_pool_recycle_seconds,
        )
    )
    repository.create_schema()
    typer.echo("Schema criado.")


if __name__ == "__main__":
    app()
