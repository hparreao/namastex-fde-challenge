from __future__ import annotations

import re
from pathlib import Path

from autoseguro.pii import find_pii


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
        assert find_pii(content) == set(), f"PII detectável em {path.name}"
        for category, pattern in patterns.items():
            assert not pattern.search(content), f"{category} encontrado em {path.name}"
