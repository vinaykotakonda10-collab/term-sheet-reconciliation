"""Environment-driven configuration.

All settings come from environment variables (optionally loaded from a local
``.env`` file). Nothing secret is hard-coded, and the API key is excluded from
``repr`` so it cannot leak through logging of the settings object.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

VALID_REASONING_EFFORTS = {"low", "medium", "high"}
VALID_DATE_ORDERS = {"auto", "MDY", "DMY"}


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Settings:
    groq_api_key: str = field(repr=False)
    groq_model: str = "openai/gpt-oss-20b"
    groq_timeout: float = 60.0
    groq_max_retries: int = 3
    groq_reasoning_effort: str | None = "low"
    groq_temperature: float = 0.0
    extraction_max_chars: int = 12_000
    extraction_max_tokens: int = 4_000
    adjudication_max_tokens: int = 1_500
    booking_date_order: str = "auto"
    log_level: str = "INFO"
    cache_dir: Path = PROJECT_ROOT / ".cache"
    output_dir: Path = PROJECT_ROOT / "outputs"
    log_dir: Path = PROJECT_ROOT / "logs"

    @property
    def masked_api_key(self) -> str:
        """Key fingerprint that is safe to display (prefix and length only)."""
        if not self.groq_api_key:
            return "<not set>"
        return f"{self.groq_api_key[:4]}…({len(self.groq_api_key)} chars)"


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


def load_settings(env_file: Path | None = None, *, require_api_key: bool = True) -> Settings:
    """Load settings from the environment, reading ``env_file`` (default ``.env``) first.

    Variables already present in the process environment take precedence over
    the file, which is the usual behaviour for CI and containers.
    """
    path = env_file or DEFAULT_ENV_FILE
    if path.exists():
        load_dotenv(path, override=False)

    api_key = os.getenv("GROQ_API_KEY", "").strip()
    if require_api_key and not api_key:
        raise ConfigError(
            "GROQ_API_KEY is not set. Copy .env.example to .env and paste your key from "
            "https://console.groq.com/keys (the .env file is git-ignored)."
        )

    effort = os.getenv("GROQ_REASONING_EFFORT", "low").strip().lower() or None
    if effort is not None and effort not in VALID_REASONING_EFFORTS:
        raise ConfigError(
            f"GROQ_REASONING_EFFORT must be one of {sorted(VALID_REASONING_EFFORTS)} or empty, got {effort!r}"
        )

    date_order = _env_str("BOOKING_DATE_ORDER", "auto")
    if date_order != "auto":
        date_order = date_order.upper()
    if date_order not in VALID_DATE_ORDERS:
        raise ConfigError(f"BOOKING_DATE_ORDER must be one of {sorted(VALID_DATE_ORDERS)}, got {date_order!r}")

    return Settings(
        groq_api_key=api_key,
        groq_model=_env_str("GROQ_MODEL", "openai/gpt-oss-20b"),
        groq_timeout=_env_float("GROQ_TIMEOUT", 60.0, minimum=1.0),
        groq_max_retries=_env_int("GROQ_MAX_RETRIES", 3, minimum=0),
        groq_reasoning_effort=effort,
        groq_temperature=_env_float("GROQ_TEMPERATURE", 0.0),
        extraction_max_chars=_env_int("EXTRACTION_MAX_CHARS", 12_000, minimum=1_000),
        extraction_max_tokens=_env_int("EXTRACTION_MAX_TOKENS", 4_000, minimum=256),
        adjudication_max_tokens=_env_int("ADJUDICATION_MAX_TOKENS", 1_500, minimum=128),
        booking_date_order=date_order,
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
    )
