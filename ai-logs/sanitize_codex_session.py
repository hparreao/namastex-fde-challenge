"""Export the visible Codex conversation without secrets or internal reasoning."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any

HOME_PATH = re.compile(r"/Users/hugoparreao")
PERSON_NAME = re.compile(r"\bHugo(?:\s+Parr[^\s/\"']+)?", re.IGNORECASE)
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
CPF = re.compile(r"(?<![A-Za-z0-9])\d{3}\.?\d{3}\.?\d{3}-?\d{2}(?![A-Za-z0-9])")
PHONE = re.compile(
    r"(?<![A-Za-z0-9])(?:\+?55\s*)?(?:\(?\d{2}\)?\s*)?"
    r"9?\d{4}[-\s]?\d{4}(?![A-Za-z0-9])"
)
CEP = re.compile(r"(?<![A-Za-z0-9])\d{5}-?\d{3}(?![A-Za-z0-9])")
PLATE = re.compile(r"(?i)\b[A-Z]{3}[-\s]?\d[A-Z0-9]\d{2}\b")
PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----.*?"
    r"-----END (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    re.DOTALL,
)
API_KEY = re.compile(r"\b(?:sk-proj-|sk-ant-)[A-Za-z0-9_-]{16,}\b")
AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")
BEARER = re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+")
SESSION_TOKEN = re.compile(
    r"(?i)(?:x-session-token|session_token)[\"']?\s*[:=]\s*[\"']?[^\s\"']+"
)
DATABASE_PASSWORD = re.compile(r"(postgresql(?:\+psycopg)?://[^:/\s]+:)[^@/\s]+(@)")
ENV_SECRET = re.compile(
    r"(?im)^(\s*(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|REDIS_PASSWORD)\s*=\s*).+$"
)
IDENTIFIER = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"|trace_[0-9a-f]{32}"
    r"|\d{8}_\d{4})(?![A-Za-z0-9])"
)


def sanitize_text(value: str) -> str:
    identifiers: list[str] = []

    def protect(match: re.Match[str]) -> str:
        identifiers.append(match.group(0))
        return f"[_SAFE_IDENTIFIER_{len(identifiers) - 1}_]"

    value = IDENTIFIER.sub(protect, value)
    value = PRIVATE_KEY.sub("[PRIVATE_KEY_REDACTED]", value)
    value = API_KEY.sub("[API_KEY_REDACTED]", value)
    value = AWS_KEY.sub("[AWS_KEY_REDACTED]", value)
    value = BEARER.sub("[AUTHORIZATION_HEADER_REDACTED]", value)
    value = SESSION_TOKEN.sub("[SESSION_AUTH_HEADER_REDACTED]", value)
    value = DATABASE_PASSWORD.sub(r"\1[DATABASE_PASSWORD_REDACTED]\2", value)
    value = ENV_SECRET.sub(r"\1[REDACTED]", value)
    value = EMAIL.sub("[EMAIL_REDACTED]", value)
    value = CPF.sub("[CPF_REDACTED]", value)
    value = PHONE.sub("[PHONE_REDACTED]", value)
    value = CEP.sub("[CEP_REDACTED]", value)
    value = PLATE.sub("[PLATE_REDACTED]", value)
    value = HOME_PATH.sub("$WORKSPACE_HOME", value)
    value = PERSON_NAME.sub("[PERSON_REDACTED]", value)
    # A second pass catches patterns that become contiguous after another replacement.
    value = EMAIL.sub("[EMAIL_REDACTED]", value)
    value = CPF.sub("[CPF_REDACTED]", value)
    value = PHONE.sub("[PHONE_REDACTED]", value)
    value = CEP.sub("[CEP_REDACTED]", value)
    value = PLATE.sub("[PLATE_REDACTED]", value)
    for index, identifier in enumerate(identifiers):
        value = value.replace(f"[_SAFE_IDENTIFIER_{index}_]", identifier)
    final_identifiers: list[str] = []

    def protect_final(match: re.Match[str]) -> str:
        final_identifiers.append(match.group(0))
        return f"[_FINAL_SAFE_IDENTIFIER_{len(final_identifiers) - 1}_]"

    value = IDENTIFIER.sub(protect_final, value)
    value = EMAIL.sub("[EMAIL_REDACTED]", value)
    value = CPF.sub("[CPF_REDACTED]", value)
    value = PHONE.sub("[PHONE_REDACTED]", value)
    value = CEP.sub("[CEP_REDACTED]", value)
    value = PLATE.sub("[PLATE_REDACTED]", value)
    for index, identifier in enumerate(final_identifiers):
        value = value.replace(f"[_FINAL_SAFE_IDENTIFIER_{index}_]", identifier)
    return value


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {
            key: "[REDACTED]"
            if key.lower() in {"encrypted_content", "api_key", "token", "session_token"}
            else sanitize(item)
            for key, item in value.items()
            if key != "internal_chat_message_metadata_passthrough"
        }
    return value


def export_session(
    source: Path, destination: Path, *, max_records: int | None = None
) -> dict[str, int | str | None]:
    counts = {"messages": 0, "tool_calls": 0, "tool_outputs": 0, "skipped": 0}
    source_hasher = hashlib.sha256()
    source_records = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")

    with source.open("rb") as source_file:
        for raw_line in source_file:
            if max_records is not None and source_records >= max_records:
                break
            source_records += 1
            source_hasher.update(raw_line)
    source_hash = source_hasher.hexdigest()

    with (
        source.open("rb") as source_file,
        temporary.open("w", encoding="utf-8") as output_file,
    ):
        metadata = {
            "kind": "export_metadata",
            "source": sanitize_text(str(source)),
            "source_sha256_at_export": source_hash,
            "source_records_at_export": source_records,
            "sanitizer": "ai-logs/sanitize_codex_session.py",
            "policy": (
                "Preserva mensagens visíveis de user/assistant, planos, chamadas de ferramentas "
                "e outputs. Exclui developer/system, reasoning, conteúdo criptografado, contagem "
                "de tokens e metadados internos; mascara secrets, credenciais e PII client-side."
            ),
        }
        output_file.write(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n"
        )

        processed_records = 0
        for raw_line in source_file:
            if processed_records >= source_records:
                break
            processed_records += 1
            event = json.loads(raw_line)
            timestamp = event.get("timestamp")
            if isinstance(timestamp, str):
                first_timestamp = first_timestamp or timestamp
                last_timestamp = timestamp
            if event.get("type") != "response_item":
                counts["skipped"] += 1
                continue
            payload = event.get("payload", {})
            payload_type = payload.get("type")
            exported: dict[str, Any] | None = None

            if payload_type == "message" and payload.get("role") in {
                "user",
                "assistant",
            }:
                exported = {
                    "timestamp": event.get("timestamp"),
                    "kind": "message",
                    "role": payload.get("role"),
                    "phase": payload.get("phase"),
                    "content": payload.get("content", []),
                }
                counts["messages"] += 1
            elif payload_type in {"custom_tool_call", "function_call"}:
                exported = {
                    "timestamp": event.get("timestamp"),
                    "kind": "tool_call",
                    "tool": payload.get("name"),
                    "call_id": payload.get("call_id"),
                    "input": payload.get("input", payload.get("arguments")),
                }
                counts["tool_calls"] += 1
            elif payload_type in {"custom_tool_call_output", "function_call_output"}:
                exported = {
                    "timestamp": event.get("timestamp"),
                    "kind": "tool_output",
                    "call_id": payload.get("call_id"),
                    "output": payload.get("output"),
                }
                counts["tool_outputs"] += 1
            else:
                counts["skipped"] += 1

            if exported is not None:
                serialized = json.dumps(
                    sanitize(exported), ensure_ascii=False, sort_keys=True
                )
                for _ in range(4):
                    next_value = sanitize_text(serialized)
                    if next_value == serialized:
                        break
                    serialized = next_value
                output_file.write(serialized + "\n")

    os.replace(temporary, destination)
    return {
        **counts,
        "source_records": source_records,
        "exported_records": counts["messages"]
        + counts["tool_calls"]
        + counts["tool_outputs"]
        + 1,
        "source_sha256": source_hash,
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--max-records", type=int)
    args = parser.parse_args()
    result = export_session(args.source, args.destination, max_records=args.max_records)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
