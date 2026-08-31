from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SessionState(StrEnum):
    QUALIFICATION = "qualification"
    PLAN_SELECTION = "plan_selection"
    CONFIRMATION = "confirmation"
    QUOTING = "quoting"
    QUOTE_PRESENTED = "quote_presented"
    COMPLETED = "completed"
    HANDOFF = "handoff"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    HANDOFF = "handoff"


class Intent(StrEnum):
    PROVIDE_DATA = "provide_data"
    CONFIRM = "confirm"
    ACCEPT = "accept"
    NEGOTIATE = "negotiate"
    HUMAN = "human"
    QUESTION = "question"
    UNKNOWN = "unknown"


class ExtractedData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    age: int | None = Field(default=None, ge=0, le=200)
    vehicle_model: str | None = Field(default=None, max_length=100)
    vehicle_year: int | None = Field(default=None, ge=1950, le=2100)
    cep_prefix: str | None = Field(default=None, pattern=r"^\d{2}$")
    plan_id: Literal["essencial", "completo", "premium"] | None = None
    start_date: date | None = None

    @field_validator("vehicle_model")
    @classmethod
    def normalize_model(cls, value: str | None) -> str | None:
        return value.strip() if value and value.strip() else None

    def merge(self, newer: ExtractedData) -> ExtractedData:
        values = self.model_dump()
        values.update(
            {key: value for key, value in newer.model_dump().items() if value is not None}
        )
        return ExtractedData.model_validate(values)


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent = Intent.UNKNOWN
    extracted: ExtractedData = Field(default_factory=ExtractedData)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class QuoteAttempt(BaseModel):
    attempt_no: int
    status: str
    duration_ms: int
    http_status: int | None = None
    error_code: str | None = None
    response_payload: dict[str, Any] | None = None


class QuotePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plano_id: Literal["essencial", "completo", "premium"]
    plano_nome: str
    premio_mensal: float = Field(gt=0)
    franquia: float = Field(ge=0)
    coberturas: list[str]
    multiplicadores: dict[str, float]
    carencia: dict[str, Any]
    moeda: Literal["BRL"]


class QuoteResult(BaseModel):
    quote_id: str
    payload: dict[str, Any]
    attempts: list[QuoteAttempt]


class HandoffInfo(BaseModel):
    reason: str
    summary: dict[str, Any]


class MessageView(BaseModel):
    id: str
    role: str
    message_type: str
    content: str
    created_at: datetime


class SessionView(BaseModel):
    id: str
    status: SessionStatus
    state: SessionState
    collected: ExtractedData
    messages: list[MessageView] = Field(default_factory=list)
    quote: dict[str, Any] | None = None
    handoff: HandoffInfo | None = None
