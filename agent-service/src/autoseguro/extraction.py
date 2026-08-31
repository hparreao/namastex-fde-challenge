from __future__ import annotations

import re
from contextlib import suppress
from datetime import date

from .domain import AgentDecision, ExtractedData, Intent
from .pii import cep_prefix_from_text

PLAN_ALIASES = {
    "essencial": "essencial",
    "completo": "completo",
    "premium": "premium",
}


def deterministic_extract(text: str) -> ExtractedData:
    lower = text.lower()
    age_match = re.search(r"\b(?:tenho|idade\s*(?:é|:)?|sou de)\s*(\d{1,3})\s*anos?\b", lower)
    year_match = re.search(r"\b((?:19|20)\d{2})\b", lower)
    plan = next((value for alias, value in PLAN_ALIASES.items() if alias in lower), None)
    date_match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", lower)
    start_date = None
    if date_match:
        with suppress(ValueError):
            start_date = date.fromisoformat(date_match.group(1))

    vehicle_model = None
    if year_match:
        prefix = text[: year_match.start()].strip(" ,.-")
        prefix = re.sub(r"(?i)^.*?(?:carro|ve[ií]culo|modelo)\s*(?:é|:)?\s*", "", prefix)
        candidate = prefix.split(",")[-1].strip()
        candidate = re.sub(r"(?i)^(?:um|uma|o|a)\s+", "", candidate)
        if candidate and len(candidate) <= 100:
            vehicle_model = candidate

    return ExtractedData(
        age=int(age_match.group(1)) if age_match else None,
        vehicle_model=vehicle_model,
        vehicle_year=int(year_match.group(1)) if year_match else None,
        cep_prefix=cep_prefix_from_text(text),
        plan_id=plan,
        start_date=start_date,
    )


def deterministic_intent(text: str) -> Intent:
    lower = text.lower().strip()
    if re.search(r"\b(humano|atendente|pessoa|vendedor)\b", lower):
        return Intent.HUMAN
    if re.search(r"\b(caro|desconto|negociar|concorrente|franquia alta|mais barato)\b", lower):
        return Intent.NEGOTIATE
    if lower in {"sim", "confirmo", "correto", "pode cotar", "isso mesmo"}:
        return Intent.CONFIRM
    if re.search(r"\b(fechar|fechado|aceito|contratar|gostei)\b", lower):
        return Intent.ACCEPT
    return Intent.PROVIDE_DATA if deterministic_extract(text) != ExtractedData() else Intent.UNKNOWN


def deterministic_decision(text: str) -> AgentDecision:
    extracted = deterministic_extract(text)
    intent = deterministic_intent(text)
    return AgentDecision(intent=intent, extracted=extracted, confidence=0.9)
