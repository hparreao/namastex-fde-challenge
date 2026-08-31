from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import uuid
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from .config import Settings, get_settings
from .coordination import (
    CoordinationBackend,
    CoordinationUnavailable,
    LocalCoordination,
    coordination_from_config,
    opaque_key,
)
from .domain import SessionView
from .errors import (
    AppError,
    CoordinationUnavailableError,
    IdempotencyConflictError,
    SessionBusyError,
    SessionNotFoundError,
)
from .logging_config import configure_logging
from .pii import find_pii
from .policy import PolicyController, policy_from_config
from .providers import LLMProvider, provider_from_settings
from .quote_client import QuoteClient
from .repository import Repository, create_db_engine
from .service import AgentService, CreateSessionResult, TurnResult
from .telemetry import (
    SafeTelemetry,
    telemetry_context,
    telemetry_from_config,
    with_repository_telemetry,
)

logger = logging.getLogger(__name__)
CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SESSION_MESSAGE_PATH = re.compile(r"^/v1/sessions/([A-Za-z0-9-]{1,64})/messages$")


class MessageInput(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    message_type: str = Field(default="text", pattern=r"^(text|image|audio|document)$")


def create_app(
    *,
    settings: Settings | None = None,
    repository: Repository | None = None,
    provider: LLMProvider | None = None,
    quote_client: QuoteClient | None = None,
    coordination: CoordinationBackend | None = None,
    telemetry: SafeTelemetry | None = None,
    policy: PolicyController | None = None,
) -> FastAPI:
    cfg = settings or get_settings()
    configure_logging(cfg.log_level)
    repo = repository or Repository(
        create_db_engine(
            cfg.database_url,
            pool_size=cfg.db_pool_size,
            max_overflow=cfg.db_max_overflow,
            pool_timeout=cfg.db_pool_timeout_seconds,
            pool_recycle=cfg.db_pool_recycle_seconds,
        )
    )
    if cfg.auto_create_schema:
        repo.create_schema()
    llm = provider or provider_from_settings(cfg)
    coordinator = coordination or coordination_from_config(
        cfg.coordination_backend,
        cfg.redis_url,
        key_prefix=cfg.redis_key_prefix,
    )
    external_trace = telemetry or telemetry_from_config(
        enabled=cfg.telemetry_enabled,
        endpoint=cfg.otel_exporter_otlp_endpoint,
        headers=cfg.otel_exporter_otlp_headers,
        service_name=cfg.telemetry_service_name,
    )
    trace = with_repository_telemetry(repo, external_trace)
    policy_controller = policy or policy_from_config(
        mode=cfg.policy_mode,
        policy_path=cfg.cedar_policy_path,
        schema_path=cfg.cedar_schema_path,
        enforce_actions=cfg.policy_enforce_actions,
        telemetry=trace,
    )
    quotes = quote_client or QuoteClient(
        cfg.quote_service_url,
        timeout_seconds=cfg.quote_timeout_seconds,
        max_attempts=cfg.quote_max_attempts,
        backoff_seconds=cfg.quote_backoff_seconds,
        coordination=coordinator,
        circuit_breaker_threshold=cfg.circuit_breaker_threshold,
        circuit_breaker_open_seconds=cfg.circuit_breaker_open_seconds,
    )
    service = AgentService(
        repo,
        llm,
        quotes,
        max_messages_per_session=cfg.max_messages_per_session,
        telemetry=trace,
        policy=policy_controller,
    )
    local_rate_fallback = LocalCoordination()

    app = FastAPI(title="AutoSeguro Agent API", version="1.0.0")
    app.state.repository = repo
    app.state.quote_client = quotes
    app.state.service = service
    app.state.coordination = coordinator
    app.state.telemetry = trace
    app.state.policy = policy_controller

    allowed_origins = [
        origin.strip() for origin in cfg.cors_allowed_origins.split(",") if origin.strip()
    ]
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=[
                "Content-Type",
                "Idempotency-Key",
                "X-Session-Token",
                "X-Correlation-ID",
            ],
        )

    @app.middleware("http")
    async def request_controls(request: Request, call_next: RequestResponseEndpoint) -> Response:
        supplied_id = request.headers.get("x-correlation-id", "")
        correlation_id = (
            supplied_id
            if CORRELATION_ID_PATTERN.fullmatch(supplied_id) and not find_pii(supplied_id)
            else str(uuid.uuid4())
        )
        request.state.correlation_id = correlation_id
        message_id = (
            str(uuid.uuid4())
            if request.method == "POST" and request.url.path.endswith("/messages")
            else None
        )
        request.state.message_id = message_id

        client_host = request.client.host if request.client else "unknown"
        create_session_path = request.method == "POST" and request.url.path == "/v1/sessions"
        limit = cfg.session_create_rate_limit if create_session_path else cfg.global_rate_limit
        bucket = "session-create" if create_session_path else "global"
        retry_after = 0
        if cfg.rate_limit_enabled:
            rate_key = opaque_key("rate", bucket, client_host)
            try:
                decision = coordinator.rate_limit(
                    rate_key,
                    limit=limit,
                    window_seconds=cfg.rate_limit_window_seconds,
                )
            except CoordinationUnavailable:
                logger.warning(
                    "rate_limit_degraded_to_local",
                    extra={"correlation_id": correlation_id, "operation": "rate_limit"},
                )
                request.state.coordination_degraded = "rate_limit_local"
                decision = local_rate_fallback.rate_limit(
                    rate_key,
                    limit=limit,
                    window_seconds=cfg.rate_limit_window_seconds,
                )
            retry_after = decision.retry_after if not decision.allowed else 0

        if retry_after:
            response: Response = JSONResponse(
                status_code=429,
                content={
                    "code": "RATE_LIMITED",
                    "message": "Muitas requisições. Tente novamente mais tarde.",
                    "correlation_id": correlation_id,
                },
                headers={"Retry-After": str(retry_after)},
            )
        else:
            if request.method == "POST" and request.url.path.endswith("/messages"):
                session_match = SESSION_MESSAGE_PATH.fullmatch(request.url.path)
                session_id = session_match.group(1) if session_match else None
                with (
                    telemetry_context(
                        correlation_id=correlation_id,
                        session_id=session_id,
                        message_id=message_id,
                    ),
                    trace.span(
                        "message",
                        {
                            "http_method": request.method,
                            "http_route": "/v1/sessions/{session_id}/messages",
                        },
                    ) as message_span,
                ):
                    response = await call_next(request)
                    message_span.set_attribute("http_status", response.status_code)
            else:
                response = await call_next(request)

        _set_security_headers(response, correlation_id, enable_hsts=cfg.enable_hsts)
        if message_id is not None:
            response.headers["X-Message-ID"] = message_id
        if degraded := getattr(request.state, "coordination_degraded", None):
            response.headers["X-Control-Degraded"] = str(degraded)
        return response

    @app.exception_handler(AppError)
    async def application_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status,
            content={
                "code": exc.code,
                "message": str(exc),
                "correlation_id": _request_id(request),
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        fields = [
            {"location": ".".join(str(part) for part in error["loc"]), "type": error["type"]}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "A requisição contém campos inválidos.",
                "fields": fields,
                "correlation_id": _request_id(request),
            },
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError) -> JSONResponse:
        logger.exception(
            "database_error",
            extra={
                "correlation_id": _request_id(request),
                "method": request.method,
                "path": request.url.path,
                "error_type": type(exc).__name__,
            },
        )
        return JSONResponse(
            status_code=503,
            content={
                "code": "DATABASE_UNAVAILABLE",
                "message": "Persistência temporariamente indisponível.",
                "correlation_id": _request_id(request),
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unexpected_error",
            extra={
                "correlation_id": _request_id(request),
                "method": request.method,
                "path": request.url.path,
                "error_type": type(exc).__name__,
            },
        )
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "Erro interno inesperado.",
                "correlation_id": _request_id(request),
            },
        )

    def authorize_session(
        session_id: str,
        session_token: Annotated[
            str | None, Header(alias="X-Session-Token", max_length=256)
        ] = None,
    ) -> None:
        with trace.span("authorization", {"session_id": session_id}) as span:
            if not session_token:
                span.set_attribute("status", "deny")
                raise SessionNotFoundError()
            repo.authorize_session(session_id, session_token)
            span.set_attribute("status", "allow")

    @app.get("/health")
    def health() -> dict[str, object]:
        database_ok = repo.ping()
        quote_ok = quotes.health()
        coordination_ok = coordinator.health()
        policy_required = policy_controller.mode == "enforce"
        policy_ok = policy_controller.engine.name != "unavailable"
        return {
            "status": (
                "ok"
                if database_ok and coordination_ok and (policy_ok or not policy_required)
                else "degraded"
            ),
            "database": "ok" if database_ok else "unavailable",
            "quote_service": "ok" if quote_ok else "unavailable",
            "coordination": coordinator.name if coordination_ok else "unavailable",
            "policy": {
                "mode": policy_controller.mode,
                "engine": policy_controller.engine.name,
            },
        }

    @app.post("/v1/sessions", response_model=CreateSessionResult, status_code=201)
    def create_session() -> CreateSessionResult:
        return service.create_session()

    @app.post("/v1/sessions/{session_id}/messages", response_model=TurnResult)
    def send_message(
        request: Request,
        session_id: str,
        body: MessageInput,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=8,
                max_length=128,
                pattern=r"^[A-Za-z0-9._:-]+$",
            ),
        ],
        _: None = Depends(authorize_session),
    ) -> TurnResult:
        fingerprint = hashlib.sha256(f"{body.message_type}\x00{body.content}".encode()).hexdigest()
        cache_key = opaque_key("idempotency", session_id, idempotency_key)
        lock_key = opaque_key("session-lock", session_id)
        try:
            with trace.span(
                "idempotency",
                {"session_id": session_id, "coordination": coordinator.name},
            ) as idempotency_span:
                cached = coordinator.get_idempotency(cache_key)
                if cached is not None:
                    idempotency_span.set_attribute("cache_status", "hit")
                    return _cached_turn(cached, fingerprint)
                idempotency_span.set_attribute("cache_status", "miss")

            with trace.span("session_lock", {"session_id": session_id}) as lock_span:
                lease = coordinator.acquire_lock(lock_key, ttl_seconds=cfg.session_lock_ttl_seconds)
                lock_span.set_attribute("status", "acquired" if lease else "waiting")
            if lease is None:
                deadline = time.monotonic() + cfg.session_lock_wait_seconds
                while time.monotonic() < deadline:
                    cached = coordinator.get_idempotency(cache_key)
                    if cached is not None:
                        return _cached_turn(cached, fingerprint)
                    time.sleep(0.02)
                raise SessionBusyError()

            try:
                cached = coordinator.get_idempotency(cache_key)
                if cached is not None:
                    return _cached_turn(cached, fingerprint)
                result = service.handle_message(session_id, body.content, body.message_type)
                coordinator.put_idempotency(
                    cache_key,
                    json.dumps(
                        {
                            "fingerprint": fingerprint,
                            "response": result.model_dump(mode="json"),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    ttl_seconds=cfg.idempotency_ttl_seconds,
                )
                return result
            finally:
                try:
                    lease.release()
                except CoordinationUnavailable:
                    request.state.coordination_degraded = "lock_release_failed"
                    logger.warning(
                        "session_lock_release_failed",
                        extra={
                            "correlation_id": _request_id(request),
                            "session_id": session_id,
                            "operation": "lock_release",
                        },
                    )
        except CoordinationUnavailable as exc:
            logger.error(
                "critical_coordination_unavailable",
                extra={
                    "correlation_id": _request_id(request),
                    "session_id": session_id,
                    "operation": str(exc),
                },
            )
            raise CoordinationUnavailableError() from exc

    @app.get("/v1/sessions/{session_id}", response_model=SessionView)
    def get_session(session_id: str, _: None = Depends(authorize_session)) -> SessionView:
        return service.get_session(session_id)

    @app.get("/v1/sessions/{session_id}/trace")
    def get_trace(session_id: str, _: None = Depends(authorize_session)) -> dict[str, object]:
        return service.trace(session_id)

    return app


def _request_id(request: Request) -> str:
    return str(getattr(request.state, "correlation_id", "unknown"))


def _cached_turn(value: str, fingerprint: str) -> TurnResult:
    parsed = json.loads(value)
    if parsed.get("fingerprint") != fingerprint:
        raise IdempotencyConflictError()
    return TurnResult.model_validate(parsed["response"])


def _set_security_headers(response: Response, correlation_id: str, *, enable_hsts: bool) -> None:
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Cache-Control"] = "no-store"
    if enable_hsts:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"


def _build_default_app() -> FastAPI:
    try:
        return create_app()
    except ValueError as exc:
        error_detail = str(exc)
        app = FastAPI(title="AutoSeguro Agent API", version="1.0.0")

        @app.get("/health", status_code=503)
        def configuration_error() -> dict[str, str]:
            return {"status": "configuration_error", "detail": error_detail}

        return app


app = _build_default_app()
