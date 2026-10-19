"""Groq Cloud client: JSON chat completions with classified errors, retries and throttling.

The pipeline depends only on the small ``LLMClient`` protocol, so another
provider can be plugged in by implementing ``complete_json``.

Reliability features
* Errors are mapped to typed ``LLMError`` subclasses (authentication, model
  unavailable, rate limit, daily quota, timeout, network, server, request too
  large, malformed output). Messages are sanitised; the API key never appears.
* Retryable failures use exponential backoff with jitter, honour the server's
  ``retry-after`` hint and stop after ``GROQ_MAX_RETRIES`` retries.
* Proactive throttling: the ``x-ratelimit-remaining-tokens`` /
  ``x-ratelimit-reset-tokens`` headers of each response are used to wait for
  the next token window instead of provoking HTTP 429s on the free tier.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Settings
from .logging_setup import redact

logger = logging.getLogger(__name__)

MAX_RETRY_WAIT_SECONDS = 90.0  # longer server-requested waits mean a daily quota is exhausted
COMPLETION_RESERVE_TOKENS = 1_200


# --------------------------------------------------------------------------- errors


class LLMError(Exception):
    category = "LLM_ERROR"
    retryable = False
    fatal = False  # fatal errors abort the run: nothing can succeed until configuration is fixed

    def __init__(self, message: str, *, status_code: int | None = None, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class LLMAuthenticationError(LLMError):
    category, fatal = "AUTHENTICATION", True


class LLMPermissionError(LLMError):
    category, fatal = "PERMISSION_DENIED", True


class LLMModelUnavailableError(LLMError):
    category, fatal = "MODEL_UNAVAILABLE", True


class LLMRateLimitError(LLMError):
    category, retryable = "RATE_LIMIT", True


class LLMQuotaExceededError(LLMError):
    category = "QUOTA_EXCEEDED"


class LLMTimeoutError(LLMError):
    category, retryable = "TIMEOUT", True


class LLMConnectionError(LLMError):
    category, retryable = "NETWORK", True


class LLMServerError(LLMError):
    category, retryable = "SERVER_ERROR", True


class LLMRequestTooLargeError(LLMError):
    category = "REQUEST_TOO_LARGE"


class LLMBadRequestError(LLMError):
    category = "BAD_REQUEST"


class LLMResponseFormatError(LLMError):
    category, retryable = "INVALID_RESPONSE", True


class LLMTruncatedResponseError(LLMResponseFormatError):
    category, retryable = "TRUNCATED_RESPONSE", False


def _error_body(exc: Any) -> tuple[str, str | None]:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            return str(err.get("message") or exc), err.get("code")
    return str(getattr(exc, "message", None) or exc), None


def _retry_after(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    value = headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return parse_duration(value)


def classify_error(exc: Exception, model: str, secrets: tuple[str, ...] = ()) -> LLMError:
    """Translate a Groq SDK exception into a sanitised, typed ``LLMError``."""
    import groq

    message, code = _error_body(exc)
    message = redact(message, secrets)[:500]
    status = getattr(exc, "status_code", None)

    if isinstance(exc, groq.APITimeoutError):
        return LLMTimeoutError(f"Groq request timed out: {message}")
    if isinstance(exc, groq.APIConnectionError):
        return LLMConnectionError(f"Could not reach the Groq API (network/DNS/proxy): {message}")
    if isinstance(exc, groq.AuthenticationError):
        return LLMAuthenticationError(
            "Groq rejected the API key (HTTP 401). Check GROQ_API_KEY in .env.", status_code=status
        )
    if isinstance(exc, groq.PermissionDeniedError):
        return LLMPermissionError(f"Access denied by Groq (HTTP 403): {message}", status_code=status)
    if isinstance(exc, groq.NotFoundError):
        return LLMModelUnavailableError(
            f"Model {model!r} does not exist or is not available to this API key (HTTP 404). "
            "Run `python test_groq_connection.py` to list available models and set GROQ_MODEL.",
            status_code=status,
        )
    if isinstance(exc, groq.RateLimitError):
        wait = _retry_after(exc)
        if wait is not None and wait > MAX_RETRY_WAIT_SECONDS:
            return LLMQuotaExceededError(
                f"Groq usage quota exhausted (HTTP 429, retry in {wait:.0f}s): {message}", status_code=status,
                retry_after=wait,
            )
        return LLMRateLimitError(f"Groq rate limit hit (HTTP 429): {message}", status_code=status, retry_after=wait)
    if isinstance(exc, groq.BadRequestError):
        if code == "json_validate_failed":
            return LLMResponseFormatError(f"Model produced invalid JSON (Groq json_validate_failed): {message}")
        return LLMBadRequestError(f"Groq rejected the request (HTTP 400): {message}", status_code=status)
    if isinstance(exc, groq.APIStatusError):
        if status == 413:
            return LLMRequestTooLargeError(
                f"Request too large for the model/rate limit (HTTP 413); lower EXTRACTION_MAX_CHARS: {message}",
                status_code=status,
            )
        if status is not None and (status >= 500 or status == 498):
            return LLMServerError(f"Groq service error (HTTP {status}): {message}", status_code=status)
        return LLMBadRequestError(f"Groq API error (HTTP {status}): {message}", status_code=status)
    return LLMError(f"Unexpected LLM client error ({type(exc).__name__}): {redact(str(exc), secrets)[:300]}")


# --------------------------------------------------------------------------- helpers

_DURATION = re.compile(
    r"^(?:(?P<h>\d+(?:\.\d+)?)h)?(?:(?P<m>\d+(?:\.\d+)?)m(?!s))?(?:(?P<s>\d+(?:\.\d+)?)s)?(?:(?P<ms>\d+(?:\.\d+)?)ms)?$"
)


def parse_duration(text: str | None) -> float | None:
    """Parse Groq header durations such as '2.28s', '1m26.4s', '7m30s' or '345ms'."""
    if not text:
        return None
    match = _DURATION.fullmatch(text.strip())
    if not match or not any(match.groupdict().values()):
        return None
    parts = {k: float(v) for k, v in match.groupdict().items() if v}
    return parts.get("h", 0) * 3600 + parts.get("m", 0) * 60 + parts.get("s", 0) + parts.get("ms", 0) / 1000


def parse_json_object(content: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences or stray prose around it."""
    text = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMResponseFormatError("response does not contain a JSON object") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMResponseFormatError(f"response is not valid JSON: {exc.msg} at char {exc.pos}") from None
    if not isinstance(parsed, dict):
        raise LLMResponseFormatError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


# --------------------------------------------------------------------------- client


@dataclass
class LLMResponse:
    content: str
    model: str
    purpose: str
    attempts: int
    latency_seconds: float
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    finish_reason: str | None = None


@dataclass
class LLMStats:
    calls: int = 0  # logical calls requested by the pipeline
    http_requests: int = 0  # including retries
    succeeded: int = 0
    failed: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    throttle_wait_seconds: float = 0.0
    failures: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "calls": self.calls,
            "http_requests": self.http_requests,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "retries": self.retries,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "throttle_wait_seconds": round(self.throttle_wait_seconds, 1),
            "failures": self.failures,
        }


class LLMClient(Protocol):
    model: str
    stats: LLMStats

    def complete_json(self, *, system: str, user: str, purpose: str, max_tokens: int) -> LLMResponse: ...


class GroqLLMClient:
    """``LLMClient`` implementation backed by the official ``groq`` SDK."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is None:
            from groq import Groq

            # SDK-level retries are disabled so that retry policy and logging live in one place.
            client = Groq(api_key=settings.groq_api_key, timeout=settings.groq_timeout, max_retries=0)
        self._client = client
        self._settings = settings
        self._sleep = sleep
        self._secrets = (settings.groq_api_key,)
        self.model = settings.groq_model
        self.stats = LLMStats()
        self._tokens_remaining: int | None = None
        self._tokens_reset_at = 0.0

    # ------------------------------------------------------------------ public

    def complete_json(self, *, system: str, user: str, purpose: str, max_tokens: int) -> LLMResponse:
        params: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self._settings.groq_temperature,
            "max_completion_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        if self._settings.groq_reasoning_effort:
            params["reasoning_effort"] = self._settings.groq_reasoning_effort
            params["include_reasoning"] = False

        estimated_prompt_tokens = (len(system) + len(user)) // 3
        self.stats.calls += 1
        attempt = 0
        while True:
            attempt += 1
            self._throttle(estimated_prompt_tokens, purpose)
            started = time.monotonic()
            try:
                self.stats.http_requests += 1
                raw = self._client.chat.completions.with_raw_response.create(**params)
                self._update_rate_limits(getattr(raw, "headers", {}) or {})
                completion = raw.parse()
                choice = completion.choices[0]
                content = choice.message.content or ""
                if choice.finish_reason == "length":
                    raise LLMTruncatedResponseError(
                        f"response truncated at max_completion_tokens={max_tokens}; raise the token limit"
                    )
                if not content.strip():
                    raise LLMResponseFormatError("model returned an empty response")
            except Exception as exc:  # noqa: BLE001 - every failure is classified below
                error = exc if isinstance(exc, LLMError) else classify_error(exc, self.model, self._secrets)
                if error.retryable and attempt <= self._settings.groq_max_retries:
                    delay = self._backoff_delay(attempt, error.retry_after)
                    self.stats.retries += 1
                    logger.warning(
                        "Groq %s call failed (%s); retry %d/%d in %.1fs",
                        purpose, error.category, attempt, self._settings.groq_max_retries, delay,
                        extra={"event": "llm_retry", "purpose": purpose, "category": error.category,
                               "attempt": attempt, "delay_s": round(delay, 2)},
                    )
                    self._sleep(delay)
                    continue
                self.stats.failed += 1
                self.stats.failures.append({"purpose": purpose, "category": error.category, "message": str(error)})
                logger.error(
                    "Groq %s call failed after %d attempt(s): %s", purpose, attempt, error,
                    extra={"event": "llm_failure", "purpose": purpose, "category": error.category,
                           "attempts": attempt},
                )
                if error is exc:
                    raise
                raise error from exc

            usage = getattr(completion, "usage", None)
            response = LLMResponse(
                content=content,
                model=getattr(completion, "model", self.model),
                purpose=purpose,
                attempts=attempt,
                latency_seconds=round(time.monotonic() - started, 2),
                prompt_tokens=getattr(usage, "prompt_tokens", None),
                completion_tokens=getattr(usage, "completion_tokens", None),
                finish_reason=choice.finish_reason,
            )
            self.stats.succeeded += 1
            self.stats.prompt_tokens += response.prompt_tokens or 0
            self.stats.completion_tokens += response.completion_tokens or 0
            logger.info(
                "Groq %s call succeeded in %.1fs (tokens in/out %s/%s, attempt %d)",
                purpose, response.latency_seconds, response.prompt_tokens, response.completion_tokens, attempt,
                extra={"event": "llm_success", "purpose": purpose, "latency_s": response.latency_seconds,
                       "prompt_tokens": response.prompt_tokens, "completion_tokens": response.completion_tokens},
            )
            return response

    def list_models(self) -> list[str]:
        try:
            return sorted(m.id for m in self._client.models.list().data)
        except Exception as exc:  # noqa: BLE001
            raise classify_error(exc, self.model, self._secrets) from exc

    # ------------------------------------------------------------------ internals

    def _backoff_delay(self, attempt: int, retry_after: float | None) -> float:
        delay = min(30.0, 1.5 * 2 ** (attempt - 1)) + random.uniform(0, 0.5)
        if retry_after is not None:
            delay = max(delay, retry_after + 0.25)
        return min(delay, MAX_RETRY_WAIT_SECONDS)

    def _update_rate_limits(self, headers: Any) -> None:
        remaining = headers.get("x-ratelimit-remaining-tokens")
        reset = parse_duration(headers.get("x-ratelimit-reset-tokens"))
        if remaining is None or reset is None:
            return
        try:
            self._tokens_remaining = int(float(remaining))
        except ValueError:
            return
        self._tokens_reset_at = time.monotonic() + reset

    def _throttle(self, estimated_prompt_tokens: int, purpose: str) -> None:
        if self._tokens_remaining is None:
            return
        now = time.monotonic()
        if now >= self._tokens_reset_at:
            self._tokens_remaining = None
            return
        needed = estimated_prompt_tokens + COMPLETION_RESERVE_TOKENS
        if needed <= self._tokens_remaining:
            return
        wait = min(self._tokens_reset_at - now + 0.5, MAX_RETRY_WAIT_SECONDS)
        logger.info(
            "Waiting %.1fs for Groq's token window before the %s call (remaining %d, needed ~%d)",
            wait, purpose, self._tokens_remaining, needed,
            extra={"event": "llm_throttle", "purpose": purpose, "wait_s": round(wait, 2)},
        )
        self.stats.throttle_wait_seconds += wait
        self._sleep(wait)
        self._tokens_remaining = None
