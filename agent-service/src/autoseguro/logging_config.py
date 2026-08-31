from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from .pii import redact_pii


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_pii(record.getMessage()),
        }
        for key in (
            "correlation_id",
            "session_id",
            "message_id",
            "quote_id",
            "error_type",
            "method",
            "path",
            "operation",
            "attempt",
            "max_attempts",
        ):
            if hasattr(record, key):
                payload[key] = redact_pii(str(getattr(record, key)))
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
