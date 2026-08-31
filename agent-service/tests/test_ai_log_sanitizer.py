from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _sanitizer() -> object:
    root = Path(__file__).parents[2]
    spec = importlib.util.spec_from_file_location(
        "sanitize_codex_session", root / "ai-logs" / "sanitize_codex_session.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sanitizer_preserves_valid_jsonl_for_escaped_content(tmp_path: Path) -> None:
    sanitizer = _sanitizer()
    source = tmp_path / "source.jsonl"
    event = {
        "timestamp": "2026-08-31T00:00:00Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [
                {"text": 'unicode café \\ regex [a-z]+ https://example.test/a?x=1\n{"nested":true}'}
            ],
        },
    }
    source.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
    destination = tmp_path / "sanitized.jsonl"
    result = sanitizer.export_session(source, destination)
    lines = destination.read_text(encoding="utf-8").splitlines()
    assert result["exported_records"] == 2
    assert all(json.loads(line) for line in lines)


def test_ai_log_lines_are_valid_json() -> None:
    root = Path(__file__).parents[2]
    exported = root / "ai-logs" / "codex-main-session-sanitized.jsonl"
    for line in exported.read_text(encoding="utf-8").splitlines():
        json.loads(line)
