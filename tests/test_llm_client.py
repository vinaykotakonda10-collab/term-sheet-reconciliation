"""Groq client behaviour with a fake SDK object: retries, error classification, throttling, JSON parsing."""

from types import SimpleNamespace

import groq
import httpx
import pytest

from src.llm_client import (
    GroqLLMClient,
    LLMAuthenticationError,
    LLMBadRequestError,
    LLMConnectionError,
    LLMModelUnavailableError,
    LLMQuotaExceededError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMTimeoutError,
    LLMTruncatedResponseError,
    parse_duration,
    parse_json_object,
)

URL = "https://api.groq.com/openai/v1/chat/completions"
REQUEST = httpx.Request("POST", URL)


def status_error(cls, status, message="error", headers=None, code=None):
    response = httpx.Response(status, request=REQUEST, headers=headers or {})
    return cls(message, response=response, body={"error": {"message": message, "code": code}})


class FakeRaw:
    def __init__(self, content='{"ok": true}', finish_reason="stop", headers=None):
        self.headers = headers or {}
        self._completion = SimpleNamespace(
            model="fake-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish_reason)],
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
        )

    def parse(self):
        return self._completion


class FakeGroq:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(with_raw_response=SimpleNamespace(create=self._create)))

    def _create(self, **params):
        self.requests.append(params)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def make_client(settings, outcomes):
    sleeps = []
    fake = FakeGroq(outcomes)
    return GroqLLMClient(settings, client=fake, sleep=sleeps.append), fake, sleeps


def call(client):
    return client.complete_json(system="s", user="u", purpose="test", max_tokens=100)


def test_successful_call_uses_json_mode_and_reasoning_settings(settings):
    client, fake, _ = make_client(settings, [FakeRaw()])
    response = call(client)
    assert response.content == '{"ok": true}' and response.prompt_tokens == 11
    params = fake.requests[0]
    assert params["response_format"] == {"type": "json_object"}
    assert params["reasoning_effort"] == "low" and params["include_reasoning"] is False
    assert params["temperature"] == 0.0
    assert client.stats.succeeded == 1 and client.stats.failed == 0


def test_rate_limit_is_retried_with_retry_after(settings):
    error = status_error(groq.RateLimitError, 429, "Rate limit reached", {"retry-after": "4"})
    client, fake, sleeps = make_client(settings, [error, FakeRaw()])
    assert call(client).content
    assert len(fake.requests) == 2 and client.stats.retries == 1
    assert sleeps[0] >= 4


def test_retries_stop_at_the_configured_maximum(settings):
    timeouts = [groq.APITimeoutError(request=REQUEST) for _ in range(3)]
    client, fake, sleeps = make_client(settings, timeouts)
    with pytest.raises(LLMTimeoutError):
        call(client)
    assert len(fake.requests) == settings.groq_max_retries + 1 == 3
    assert sleeps[1] > sleeps[0]  # exponential backoff
    assert client.stats.failed == 1 and client.stats.failures[0]["category"] == "TIMEOUT"


@pytest.mark.parametrize(
    "error,expected",
    [
        (status_error(groq.AuthenticationError, 401, "Invalid API Key"), LLMAuthenticationError),
        (status_error(groq.NotFoundError, 404, "model not found"), LLMModelUnavailableError),
        (status_error(groq.BadRequestError, 400, "bad param"), LLMBadRequestError),
        (status_error(groq.RateLimitError, 429, "tokens per day", {"retry-after": "3600"}), LLMQuotaExceededError),
    ],
)
def test_non_retryable_errors_fail_immediately(settings, error, expected):
    client, fake, sleeps = make_client(settings, [error, FakeRaw()])
    with pytest.raises(expected):
        call(client)
    assert len(fake.requests) == 1 and sleeps == []


def test_fatal_flags():
    assert LLMAuthenticationError("x").fatal and LLMModelUnavailableError("x").fatal
    assert not LLMRateLimitError("x").fatal and LLMRateLimitError("x").retryable


def test_network_errors_and_invalid_json_generation_are_retryable(settings):
    outcomes = [
        groq.APIConnectionError(request=REQUEST),
        status_error(groq.BadRequestError, 400, "Failed to generate JSON", code="json_validate_failed"),
        FakeRaw(),
    ]
    client, fake, _ = make_client(settings, outcomes)
    assert call(client).content
    assert client.stats.retries == 2


def test_connection_error_after_retries(settings):
    client, _, _ = make_client(settings, [groq.APIConnectionError(request=REQUEST) for _ in range(3)])
    with pytest.raises(LLMConnectionError, match="network"):
        call(client)


def test_truncated_and_empty_responses(settings):
    client, _, _ = make_client(settings, [FakeRaw(finish_reason="length")])
    with pytest.raises(LLMTruncatedResponseError):
        call(client)
    client, fake, _ = make_client(settings, [FakeRaw(content=""), FakeRaw()])
    assert call(client).content and len(fake.requests) == 2


def test_error_messages_never_contain_the_api_key(settings):
    error = status_error(groq.BadRequestError, 400, f"bad key {settings.groq_api_key}")
    client, _, _ = make_client(settings, [error])
    with pytest.raises(LLMBadRequestError) as excinfo:
        call(client)
    assert settings.groq_api_key not in str(excinfo.value)


def test_proactive_throttling_uses_rate_limit_headers(settings):
    headers = {"x-ratelimit-remaining-tokens": "100", "x-ratelimit-reset-tokens": "20s"}
    client, _, sleeps = make_client(settings, [FakeRaw(headers=headers), FakeRaw()])
    call(client)
    call(client)
    assert len(sleeps) == 1 and 20 <= sleeps[0] <= 21
    assert client.stats.throttle_wait_seconds > 0


def test_parse_duration():
    assert parse_duration("2.28s") == pytest.approx(2.28)
    assert parse_duration("1m26.4s") == pytest.approx(86.4)
    assert parse_duration("345ms") == pytest.approx(0.345)
    assert parse_duration("7m30s") == pytest.approx(450)
    assert parse_duration("garbage") is None


def test_parse_json_object_recovers_from_fences_and_prose():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here you go: {"a": 1} thanks') == {"a": 1}
    for bad in ("no json here", "[1, 2]", '{"a": }'):
        with pytest.raises(LLMResponseFormatError):
            parse_json_object(bad)
