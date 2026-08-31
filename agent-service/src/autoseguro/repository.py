from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session, selectinload, sessionmaker

from .domain import ExtractedData, QuoteAttempt, SessionState, SessionStatus
from .errors import SessionNotFoundError
from .models import (
    Base,
    HandoffRecord,
    MessageRecord,
    QuoteAttemptRecord,
    SessionRecord,
    TraceEventRecord,
    TransitionRecord,
)
from .security import token_matches


def create_db_engine(
    database_url: str,
    *,
    pool_size: int = 5,
    max_overflow: int = 5,
    pool_timeout: float = 3.0,
    pool_recycle: int = 1800,
) -> Engine:
    options: dict[str, Any] = {"pool_pre_ping": True}
    if not database_url.startswith("sqlite"):
        options.update(
            pool_size=pool_size,
            max_overflow=max_overflow,
            pool_timeout=pool_timeout,
            pool_recycle=pool_recycle,
        )
    return create_engine(database_url, **options)


class Repository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def create_schema(self) -> None:
        Base.metadata.create_all(self.engine)

    def ping(self) -> bool:
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except Exception:
            return False

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.session_factory() as db, db.begin():
            yield db

    def create_session(self, session_token_hash: str) -> SessionRecord:
        record = SessionRecord(
            id=str(uuid.uuid4()),
            status=SessionStatus.ACTIVE.value,
            state=SessionState.QUALIFICATION.value,
            collected=ExtractedData().model_dump(mode="json"),
            session_token_hash=session_token_hash,
        )
        with self.transaction() as db:
            db.add(record)
        return self.get_session(record.id)

    def get_session(self, session_id: str) -> SessionRecord:
        statement = (
            select(SessionRecord)
            .where(SessionRecord.id == session_id)
            .options(
                selectinload(SessionRecord.messages),
                selectinload(SessionRecord.quote_attempts),
                selectinload(SessionRecord.transitions),
                selectinload(SessionRecord.handoff),
            )
        )
        with self.session_factory() as db:
            record = db.scalar(statement)
            if record is None:
                raise SessionNotFoundError()
            db.expunge(record)
            return record

    def authorize_session(self, session_id: str, token: str) -> None:
        with self.session_factory() as db:
            token_hash = db.scalar(
                select(SessionRecord.session_token_hash).where(SessionRecord.id == session_id)
            )
        if not token_matches(token, token_hash):
            raise SessionNotFoundError()

    def add_message(
        self, session_id: str, role: str, message_type: str, content: str
    ) -> MessageRecord:
        record = MessageRecord(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role=role,
            message_type=message_type,
            content=content,
        )
        with self.transaction() as db:
            db.add(record)
        return record

    def update_session(
        self,
        session_id: str,
        *,
        state: SessionState | None = None,
        status: SessionStatus | None = None,
        collected: ExtractedData | None = None,
        clarification_count: int | None = None,
        quote_payload: dict[str, Any] | None = None,
        event: str | None = None,
    ) -> None:
        with self.transaction() as db:
            record = db.get(SessionRecord, session_id)
            if record is None:
                raise SessionNotFoundError()
            old_state = record.state
            if state is not None:
                record.state = state.value
            if status is not None:
                record.status = status.value
            if collected is not None:
                record.collected = collected.model_dump(mode="json")
            if clarification_count is not None:
                record.clarification_count = clarification_count
            if quote_payload is not None:
                record.quote_payload = quote_payload
            if state is not None and state.value != old_state:
                db.add(
                    TransitionRecord(
                        session_id=session_id,
                        from_state=old_state,
                        to_state=state.value,
                        event=event or "state_changed",
                    )
                )

    def add_quote_attempts(
        self, session_id: str, quote_id: str, attempts: list[QuoteAttempt]
    ) -> None:
        with self.transaction() as db:
            for attempt in attempts:
                db.add(
                    QuoteAttemptRecord(
                        session_id=session_id,
                        quote_id=quote_id,
                        **attempt.model_dump(),
                    )
                )

    def add_handoff(self, session_id: str, reason: str, summary: dict[str, Any]) -> None:
        with self.transaction() as db:
            db.add(HandoffRecord(session_id=session_id, reason=reason, summary=summary))

    def add_trace_event(
        self,
        session_id: str,
        *,
        correlation_id: str | None,
        span_name: str,
        status: str,
        duration_ms: int,
        attributes: dict[str, Any],
    ) -> None:
        with self.transaction() as db:
            db.add(
                TraceEventRecord(
                    session_id=session_id,
                    correlation_id=correlation_id,
                    span_name=span_name,
                    status=status,
                    duration_ms=duration_ms,
                    attributes=attributes,
                )
            )

    def list_trace_events(self, session_id: str) -> list[TraceEventRecord]:
        with self.session_factory() as db:
            records = list(
                db.scalars(
                    select(TraceEventRecord)
                    .where(TraceEventRecord.session_id == session_id)
                    .order_by(TraceEventRecord.id)
                )
            )
            for record in records:
                db.expunge(record)
            return records
