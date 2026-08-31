from __future__ import annotations

import re
from contextlib import suppress
from datetime import date
from typing import Literal, cast

from .domain import AgentDecision, ExtractedData, Intent
from .pii import cep_prefix_from_text

PLAN_ALIASES = {
    "essencial": "essencial",
    "completo": "completo",
    "premium": "premium",
}


def deterministic_extract(text: str) -> ExtractedData:
    lower = text.lower()
    age_matches = list(
        re.finditer(
            r"\b(?:tenho\s+(\d{1,3})\s*anos?|(?:minha\s+)?idade\s*(?:é|:)?"
            r"\s*(\d{1,3})|sou de\s+(\d{1,3})\s*anos?)\b",
            lower,
        )
    )
    # The final explicit age is the correction when a lead negates an earlier value.
    age_match = age_matches[-1] if age_matches else None
    year_match = re.search(r"\b((?:19|20)\d{2})\b", lower)
    plan_matches = [
        (match.start(), value)
        for alias, value in PLAN_ALIASES.items()
        for match in re.finditer(rf"\b{re.escape(alias)}\b", lower)
    ]
    # Prefer the final named plan: "não quero completo; quero premium" is a correction.
    plan = cast(
        Literal["essencial", "completo", "premium"] | None,
        max(plan_matches, default=(0, None))[1],
    )
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
        age=int(next(value for value in age_match.groups() if value is not None))
        if age_match
        else None,
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
    if _has_negation(lower):
        return (
            Intent.PROVIDE_DATA
            if deterministic_extract(text) != ExtractedData()
            else Intent.UNKNOWN
        )
    if _has_correction(lower):
        return Intent.PROVIDE_DATA
    if lower in {"sim", "confirmo", "correto", "pode cotar", "isso mesmo", "confirmo os dados"}:
        return Intent.CONFIRM
    if lower in {"aceito", "fechado", "quero contratar", "quero fechar", "aceito a proposta"}:
        return Intent.ACCEPT
    return Intent.PROVIDE_DATA if deterministic_extract(text) != ExtractedData() else Intent.UNKNOWN


def deterministic_decision(text: str) -> AgentDecision:
    extracted = deterministic_extract(text)
    intent = deterministic_intent(text)
    return AgentDecision(intent=intent, extracted=extracted, confidence=0.9)


def _has_negation(lower: str) -> bool:
    return bool(
        re.search(
            r"\bn[aã]o\s+(?:aceito|quero|confirmo|vou|pretendo|desejo|fechar|contratar)\b",
            lower,
        )
    )


def _has_correction(lower: str) -> bool:
    return bool(
        re.search(
            r"\b(?:mas\s+minha\s+idade|n[aã]o\s+tenho\s+\d+\s+anos?.*\btenho\s+\d+\s+anos?)\b",
            lower,
        )
        or re.search(r"\bn[aã]o\s+quero\s+o\s+plano\s+\w+.*\bquero\s+o\s+\w+\b", lower)
    )
