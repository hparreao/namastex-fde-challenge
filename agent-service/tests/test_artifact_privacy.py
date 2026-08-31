from __future__ import annotations

import json
import re
from pathlib import Path

from autoseguro.pii import find_pii


def _string_values(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for child in value for item in _string_values(child)]
    if isinstance(value, dict):
        return [
            item
            for key, child in value.items()
            for item in [*_string_values(str(key)), *_string_values(child)]
        ]
    return []


def test_generated_artifacts_contain_no_known_raw_pii_or_local_home_path() -> None:
    repository_root = Path(__file__).parents[2]
    files = [
        *sorted(
            path
            for pattern in ("*.json", "*.jsonl")
            for path in (repository_root / "artifacts").rglob(pattern)
            if "backups" not in path.parts and "release-readiness" not in path.parts
        ),
    ]
    main_session = repository_root / "ai-logs" / "codex-main-session-sanitized.jsonl"
    if main_session.exists():
        files.append(main_session)
    patterns = {
        "email": re.compile(r"\bteste@example\.com\b", re.IGNORECASE),
        "cpf": re.compile(r"\b389\.083\.863-43\b"),
        "home_path": re.compile(r"/Users/hugoparreao"),
        "openai_key": re.compile(r"\bsk-proj-[A-Za-z0-9_-]{16,}\b"),
        "anthropic_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),
    }
    for path in files:
        content = path.read_text(encoding="utf-8")
        values = [content]
        if path.suffix == ".jsonl":
            parsed_lines = [json.loads(line) for line in content.splitlines()]
            values = [item for parsed in parsed_lines for item in _string_values(parsed)]
        assert all(not find_pii(value) for value in values), f"PII detectável em {path.name}"
        for category, pattern in patterns.items():
            assert not pattern.search(content), f"{category} encontrado em {path.name}"
        if path.suffix == ".jsonl":
            for _line_number, line in enumerate(content.splitlines(), start=1):
                json.loads(line)
