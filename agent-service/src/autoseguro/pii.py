from __future__ import annotations

import re

IDENTIFIER_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|trace_[0-9a-f]{32}"
    r"|\d{8}_\d{4})(?![A-Za-z0-9])"
)

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EMAIL", re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")),
    ("CPF", re.compile(r"(?<![A-Za-z0-9])\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?![A-Za-z0-9])")),
    (
        "PHONE",
        re.compile(
            r"(?<![A-Za-z0-9])(?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?"
            r"9?\d{4}[-\s]?\d{4}(?![A-Za-z0-9])"
        ),
    ),
    ("CEP", re.compile(r"(?<![A-Za-z0-9])\d{5}-?\d{3}(?![A-Za-z0-9])")),
    ("PLATE", re.compile(r"(?i)\b[A-Z]{3}[-\s]?\d[A-Z0-9]\d{2}\b")),
)


def redact_pii(text: str) -> str:
    identifiers: list[str] = []

    def protect(match: re.Match[str]) -> str:
        identifiers.append(match.group(0))
        return f"[_SAFE_IDENTIFIER_{len(identifiers) - 1}_]"

    sanitized = IDENTIFIER_PATTERN.sub(protect, text)
    for label, pattern in PATTERNS:
        sanitized = pattern.sub(f"[{label}_REDACTED]", sanitized)
    for index, identifier in enumerate(identifiers):
        sanitized = sanitized.replace(f"[_SAFE_IDENTIFIER_{index}_]", identifier)
    return sanitized


def find_pii(text: str) -> set[str]:
    candidate = IDENTIFIER_PATTERN.sub("[SAFE_IDENTIFIER]", text)
    return {label for label, pattern in PATTERNS if pattern.search(candidate)}


def cep_prefix_from_text(text: str) -> str | None:
    candidate = IDENTIFIER_PATTERN.sub("[SAFE_IDENTIFIER]", text)
    match = dict(PATTERNS)["CEP"].search(candidate)
    return match.group(0)[:2] if match else None
