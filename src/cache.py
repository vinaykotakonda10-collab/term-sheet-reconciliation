"""Small content-addressed JSON cache for LLM responses.

Keys are SHA-256 hashes over everything that influences a response (model,
prompt version, full prompt text – which embeds the document text). A changed
document, prompt or model therefore never reuses a stale answer. Only raw LLM
output is cached; it is re-validated on every read, so the cache can never
bypass schema validation or evidence checks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


class JsonCache:
    def __init__(self, directory: Path, *, enabled: bool = True) -> None:
        self.directory = directory
        self.enabled = enabled
        self.hits = 0
        self.writes = 0

    @staticmethod
    def make_key(*parts: object) -> str:
        blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _path(self, namespace: str, key: str) -> Path:
        return self.directory / namespace / f"{key}.json"

    def get(self, namespace: str, key: str) -> dict | None:
        if not self.enabled:
            return None
        path = self._path(namespace, key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Ignoring unreadable cache entry %s: %s", path.name, exc)
            return None
        self.hits += 1
        return payload

    def set(self, namespace: str, key: str, payload: dict) -> None:
        if not self.enabled:
            return
        path = self._path(namespace, key)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"cached_at": datetime.now(timezone.utc).isoformat(), **payload}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
        self.writes += 1
