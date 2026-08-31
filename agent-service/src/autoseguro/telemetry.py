from __future__ import annotations

import hashlib
import logging
import re
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from .pii import find_pii, redact_pii

if TYPE_CHECKING:
    from .repository import Repository

logger = logging.getLogger(__name__)
_TRACE_CONTEXT: ContextVar[dict[str, str] | None] = ContextVar(
    "autoseguro_trace_context", default=None
)
BLOCKED_ATTRIBUTE_PARTS = {"token", "prompt", "response", "content", "message", "secret", "key"}
EXPLICIT_SAFE_ATTRIBUTES = {
    "prompt_version",
    "input_tokens",
    "output_tokens",
    "total_tokens",
}
IDENTIFIER_ATTRIBUTES = {
    "canonical_trace_id",
    "correlation_id",
    "message_id",
    "quote_id",
    "session_id",
    "trace_id",
}
CANONICAL_TRACE_PATTERN = re.compile(r"^trace_[0-9a-f]{32}$")
CLIENT_CORRELATION_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9._:-]{0,127}$")


class SpanHandle(Protocol):
    def set_attribute(self, key: str, value: str | int | float | bool | None) -> None: ...

    def add_event(
        self, name: str, attributes: Mapping[str, str | int | float | bool | None] | None = None
    ) -> None: ...


class TelemetryBackend(Protocol):
    @contextmanager
    def span(
        self, name: str, attributes: Mapping[str, str | int | float | bool]
    ) -> Iterator[SpanHandle]: ...


class _NoopSpan:
    def set_attribute(self, key: str, value: str | int | float | bool | None) -> None:
        del key, value

    def add_event(
        self, name: str, attributes: Mapping[str, str | int | float | bool | None] | None = None
    ) -> None:
        del name, attributes


class NoopTelemetryBackend:
    @contextmanager
    def span(
        self, name: str, attributes: Mapping[str, str | int | float | bool]
    ) -> Iterator[SpanHandle]:
        del name, attributes
        yield _NoopSpan()


@dataclass
class SpanRecord:
    name: str
    attributes: dict[str, str | int | float | bool] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    status: str = "started"
    duration_ms: float = 0.0


class _MemorySpan:
    def __init__(self, record: SpanRecord) -> None:
        self.record = record

    def set_attribute(self, key: str, value: str | int | float | bool | None) -> None:
        if value is not None:
            self.record.attributes[key] = value

    def add_event(
        self, name: str, attributes: Mapping[str, str | int | float | bool | None] | None = None
    ) -> None:
        self.record.events.append(
            {
                "name": name,
                "attributes": {k: v for k, v in (attributes or {}).items() if v is not None},
            }
        )


class InMemoryTelemetryBackend:
    def __init__(self) -> None:
        self.records: list[SpanRecord] = []

    @contextmanager
    def span(
        self, name: str, attributes: Mapping[str, str | int | float | bool]
    ) -> Iterator[SpanHandle]:
        record = SpanRecord(name=name, attributes=dict(attributes))
        self.records.append(record)
        started = time.monotonic()
        try:
            yield _MemorySpan(record)
        except Exception:
            record.status = "error"
            raise
        else:
            record.status = "ok"
        finally:
            record.duration_ms = (time.monotonic() - started) * 1000


class RepositoryTelemetryBackend:
    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    @contextmanager
    def span(
        self, name: str, attributes: Mapping[str, str | int | float | bool]
    ) -> Iterator[SpanHandle]:
        record = SpanRecord(name=name, attributes=dict(attributes))
        started = time.monotonic()
        try:
            yield _MemorySpan(record)
        except Exception:
            record.status = "error"
            raise
        else:
            record.status = "ok"
        finally:
            record.duration_ms = (time.monotonic() - started) * 1000
            session_id = record.attributes.get("session_id")
            if isinstance(session_id, str):
                self.repository.add_trace_event(
                    session_id,
                    correlation_id=(
                        str(record.attributes["correlation_id"])
                        if "correlation_id" in record.attributes
                        else None
                    ),
                    span_name=record.name,
                    status=record.status,
                    duration_ms=round(record.duration_ms),
                    attributes={
                        **record.attributes,
                        "events": record.events,
                    },
                )


class _CompositeSpan:
    def __init__(self, handles: list[SpanHandle]) -> None:
        self.handles = handles

    def set_attribute(self, key: str, value: str | int | float | bool | None) -> None:
        for handle in self.handles:
            try:
                handle.set_attribute(key, value)
            except Exception as exc:
                logger.warning(
                    "telemetry_backend_attribute_failed",
                    extra={"error_type": type(exc).__name__},
                )

    def add_event(
        self, name: str, attributes: Mapping[str, str | int | float | bool | None] | None = None
    ) -> None:
        for handle in self.handles:
            try:
                handle.add_event(name, attributes)
            except Exception as exc:
                logger.warning(
                    "telemetry_backend_event_failed",
                    extra={"error_type": type(exc).__name__},
                )


class CompositeTelemetryBackend:
    def __init__(self, backends: list[TelemetryBackend]) -> None:
        self.backends = backends

    @contextmanager
    def span(
        self, name: str, attributes: Mapping[str, str | int | float | bool]
    ) -> Iterator[SpanHandle]:
        contexts: list[Any] = []
        handles: list[SpanHandle] = []
        for backend in self.backends:
            try:
                context = backend.span(name, attributes)
                handles.append(context.__enter__())
                contexts.append(context)
            except Exception as exc:
                logger.warning(
                    "telemetry_backend_start_failed",
                    extra={"error_type": type(exc).__name__},
                )
        try:
            yield _CompositeSpan(handles)
        except BaseException as application_error:
            for context in reversed(contexts):
                try:
                    context.__exit__(
                        type(application_error),
                        application_error,
                        application_error.__traceback__,
                    )
                except Exception as exc:
                    logger.warning(
                        "telemetry_backend_finish_failed",
                        extra={"error_type": type(exc).__name__},
                    )
            raise
        else:
            for context in reversed(contexts):
                try:
                    context.__exit__(None, None, None)
                except Exception as exc:
                    logger.warning(
                        "telemetry_backend_finish_failed",
                        extra={"error_type": type(exc).__name__},
                    )


class _OpenTelemetrySpan:
    def __init__(self, span: Any) -> None:
        self.span = span

    def set_attribute(self, key: str, value: str | int | float | bool | None) -> None:
        if value is not None:
            self.span.set_attribute(key, value)

    def add_event(
        self, name: str, attributes: Mapping[str, str | int | float | bool | None] | None = None
    ) -> None:
        self.span.add_event(name, {k: v for k, v in (attributes or {}).items() if v is not None})


class OpenTelemetryBackend:
    def __init__(self, endpoint: str, headers: str, service_name: str) -> None:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        parsed_headers = dict(part.split("=", 1) for part in headers.split(",") if "=" in part)
        exporter = OTLPSpanExporter(endpoint=endpoint, headers=parsed_headers, timeout=1)
        provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
        provider.add_span_processor(BatchSpanProcessor(exporter))
        self.provider = provider
        self.tracer = provider.get_tracer(service_name)

    @contextmanager
    def span(
        self, name: str, attributes: Mapping[str, str | int | float | bool]
    ) -> Iterator[SpanHandle]:
        with self.tracer.start_as_current_span(name, attributes=dict(attributes)) as span:
            yield _OpenTelemetrySpan(span)


class SafeTelemetry:
    """Client-side masking and failure isolation around any telemetry backend."""

    def __init__(self, backend: TelemetryBackend | None = None) -> None:
        self.backend = backend or NoopTelemetryBackend()

    @contextmanager
    def span(
        self,
        name: str,
        attributes: Mapping[str, str | int | float | bool | None] | None = None,
    ) -> Iterator[SpanHandle]:
        safe = _safe_attributes({**(_TRACE_CONTEXT.get() or {}), **(attributes or {})})
        context: Any = None
        handle: SpanHandle = _NoopSpan()
        try:
            context = self.backend.span(name, safe)
            handle = context.__enter__()
        except Exception as exc:
            logger.warning("telemetry_start_failed", extra={"error_type": type(exc).__name__})
            context = None

        safe_handle = _SafeSpan(handle)
        try:
            yield safe_handle
        except BaseException as application_error:
            if context is not None:
                try:
                    context.__exit__(
                        type(application_error), application_error, application_error.__traceback__
                    )
                except Exception as exc:
                    logger.warning(
                        "telemetry_finish_failed", extra={"error_type": type(exc).__name__}
                    )
            raise
        else:
            if context is not None:
                try:
                    context.__exit__(None, None, None)
                except Exception as exc:
                    logger.warning(
                        "telemetry_finish_failed", extra={"error_type": type(exc).__name__}
                    )


class _SafeSpan:
    def __init__(self, handle: SpanHandle) -> None:
        self.handle = handle

    def set_attribute(self, key: str, value: str | int | float | bool | None) -> None:
        safe = _safe_attributes({key: value})
        if key not in safe:
            return
        try:
            self.handle.set_attribute(key, safe[key])
        except Exception as exc:
            logger.warning("telemetry_attribute_failed", extra={"error_type": type(exc).__name__})

    def add_event(
        self, name: str, attributes: Mapping[str, str | int | float | bool | None] | None = None
    ) -> None:
        try:
            self.handle.add_event(name, _safe_attributes(attributes or {}))
        except Exception as exc:
            logger.warning("telemetry_event_failed", extra={"error_type": type(exc).__name__})


def _safe_attributes(
    attributes: Mapping[str, str | int | float | bool | None],
) -> dict[str, str | int | float | bool]:
    safe: dict[str, str | int | float | bool] = {}
    for key, value in attributes.items():
        normalized = key.lower().replace("-", "_")
        if (
            normalized not in EXPLICIT_SAFE_ATTRIBUTES
            and normalized not in IDENTIFIER_ATTRIBUTES
            and any(part in normalized for part in BLOCKED_ATTRIBUTE_PARTS)
        ) or value is None:
            continue
        if isinstance(value, str):
            safe[key] = (
                value[:256] if _safe_identifier(normalized, value) else redact_pii(value)[:256]
            )
        else:
            safe[key] = value
    return safe


def _safe_identifier(key: str, value: str) -> bool:
    if key not in IDENTIFIER_ATTRIBUTES:
        return False
    if key == "canonical_trace_id":
        return bool(CANONICAL_TRACE_PATTERN.fullmatch(value))
    try:
        uuid.UUID(value)
    except ValueError:
        return key == "correlation_id" and bool(
            CLIENT_CORRELATION_PATTERN.fullmatch(value) and not find_pii(value)
        )
    return True


def canonical_trace_id(correlation_id: str) -> str:
    """Return an Agents SDK-compatible opaque ID without exposing request data."""
    digest = hashlib.sha256(correlation_id.encode("utf-8")).hexdigest()[:32]
    return f"trace_{digest}"


@contextmanager
def telemetry_context(
    *,
    correlation_id: str,
    session_id: str | None = None,
    message_id: str | None = None,
) -> Iterator[str]:
    trace_id = canonical_trace_id(correlation_id)
    values = {"correlation_id": correlation_id, "canonical_trace_id": trace_id}
    if session_id is not None:
        values["session_id"] = session_id
    if message_id is not None:
        values["message_id"] = message_id
    token: Token[dict[str, str] | None] = _TRACE_CONTEXT.set(values)
    try:
        yield trace_id
    finally:
        _TRACE_CONTEXT.reset(token)


def current_canonical_trace_id() -> str | None:
    return (_TRACE_CONTEXT.get() or {}).get("canonical_trace_id")


def telemetry_from_config(
    *, enabled: bool, endpoint: str, headers: str, service_name: str
) -> SafeTelemetry:
    if not enabled:
        return SafeTelemetry()
    try:
        return SafeTelemetry(OpenTelemetryBackend(endpoint, headers, service_name))
    except Exception as exc:
        logger.warning("telemetry_initialization_failed", extra={"error_type": type(exc).__name__})
        return SafeTelemetry()


def with_repository_telemetry(repository: Repository, external: SafeTelemetry) -> SafeTelemetry:
    return SafeTelemetry(
        CompositeTelemetryBackend([RepositoryTelemetryBackend(repository), external.backend])
    )
