"""Standalone Groq Cloud connectivity check.

Loads GROQ_API_KEY from .env, makes one minimal JSON-mode completion with the
configured model and reports the outcome. Errors are classified (authentication,
model unavailable, rate limit / quota, network, ...) and printed without
revealing the key.

    python test_groq_connection.py            # uses GROQ_MODEL from .env
    python test_groq_connection.py --model openai/gpt-oss-120b
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

from src.config import ConfigError, load_settings
from src.llm_client import (
    GroqLLMClient,
    LLMAuthenticationError,
    LLMConnectionError,
    LLMError,
    LLMModelUnavailableError,
    LLMQuotaExceededError,
    LLMRateLimitError,
    LLMTimeoutError,
    parse_json_object,
)

HINTS = {
    LLMAuthenticationError: "Create a key at https://console.groq.com/keys and set GROQ_API_KEY in .env.",
    LLMModelUnavailableError: "Pick a model from the list above and set GROQ_MODEL in .env.",
    LLMRateLimitError: "Free-tier rate limit reached; wait a minute and retry.",
    LLMQuotaExceededError: "Daily free-tier quota exhausted; retry later or use another model.",
    LLMConnectionError: "Check internet access, proxy and firewall settings for api.groq.com.",
    LLMTimeoutError: "The API did not answer in time; retry or raise GROQ_TIMEOUT.",
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify the Groq API key and model with one live request.")
    parser.add_argument("--model", help="Model to test instead of GROQ_MODEL")
    args = parser.parse_args()

    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[FAIL] Configuration: {exc}")
        return 2
    if args.model:
        settings = replace(settings, groq_model=args.model)
    # One retry is enough for a connectivity check.
    settings = replace(settings, groq_max_retries=min(settings.groq_max_retries, 1))

    print(f"API key : {settings.masked_api_key}")
    print(f"Model   : {settings.groq_model}")
    client = GroqLLMClient(settings)

    try:
        models = client.list_models()
        available = settings.groq_model in models
        print(f"Models  : {len(models)} available to this key; configured model listed: {'yes' if available else 'NO'}")
        if not available:
            print("          available: " + ", ".join(models))
    except LLMError as exc:
        print(f"[FAIL] Listing models failed ({exc.category}): {exc}")
        hint = next((h for cls, h in HINTS.items() if isinstance(exc, cls)), None)
        if hint:
            print(f"       Hint: {hint}")
        return 1

    try:
        response = client.complete_json(
            system="You are a connectivity check. Reply with JSON only.",
            user='Return exactly {"status": "ok"}',
            purpose="connection_test",
            max_tokens=200,
        )
        payload = parse_json_object(response.content)
    except LLMError as exc:
        print(f"[FAIL] Completion request failed ({exc.category}): {exc}")
        hint = next((h for cls, h in HINTS.items() if isinstance(exc, cls)), None)
        if hint:
            print(f"       Hint: {hint}")
        return 1

    print(f"[OK]   Live completion succeeded with model {response.model} in {response.latency_seconds}s "
          f"(tokens in/out {response.prompt_tokens}/{response.completion_tokens}); reply: {payload}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
