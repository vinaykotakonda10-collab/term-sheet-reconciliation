"""Structured logging with secret redaction.

Console output is human-readable; a JSON-lines file in ``logs/`` captures the
same events with structured ``extra`` fields for later analysis. A filter
redacts API keys from every record before any handler formats it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

_GROQ_KEY_PATTERN = re.compile(r"gsk_[A-Za-z0-9]{8,}")
_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


def redact(text: str, secrets: Iterable[str] = ()) -> str:
    """Mask known secrets and anything shaped like a Groq API key."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***REDACTED***")
    return _GROQ_KEY_PATTERN.sub("gsk_***REDACTED***", text)


class SecretRedactionFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage(), self._secrets)
        record.args = None
        for key, value in list(record.__dict__.items()):
            if key not in _STANDARD_ATTRS and isinstance(value, str):
                record.__dict__[key] = redact(value, self._secrets)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", log_dir: Path | None = None, secrets: Iterable[str] = ()) -> Path | None:
    """Configure root logging; returns the JSON log file path when ``log_dir`` is given."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    redaction = SecretRedactionFilter(secrets)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    console.addFilter(redaction)
    root.addHandler(console)

    log_path = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"run_{datetime.now():%Y%m%d_%H%M%S}.jsonl"
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(JsonFormatter())
        file_handler.addFilter(redaction)
        root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "groq"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return log_path
