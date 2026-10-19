import json
import logging

import pytest

import main
from src.config import ConfigError, load_settings
from src.logging_setup import configure_logging, redact

ENV_VARS = ("GROQ_API_KEY", "GROQ_MODEL", "GROQ_TIMEOUT", "GROQ_MAX_RETRIES", "GROQ_REASONING_EFFORT",
            "BOOKING_DATE_ORDER", "EXTRACTION_MAX_CHARS")


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    # setenv first so monkeypatch also undoes values that load_dotenv writes during the test.
    for name in ENV_VARS:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    return tmp_path / "missing.env"


def test_missing_key_is_a_clear_config_error(clean_env):
    with pytest.raises(ConfigError, match="GROQ_API_KEY is not set"):
        load_settings(clean_env)
    assert load_settings(clean_env, require_api_key=False).groq_model == "openai/gpt-oss-20b"


def test_env_file_values_and_validation(clean_env, monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("GROQ_API_KEY=gsk_abcdefghijklmnop\nGROQ_MODEL=openai/gpt-oss-120b\nGROQ_MAX_RETRIES=5\n")
    settings = load_settings(env)
    assert settings.groq_model == "openai/gpt-oss-120b" and settings.groq_max_retries == 5
    assert "gsk_abcdefghijklmnop" not in repr(settings)
    assert settings.masked_api_key.startswith("gsk_") and "abcdefgh" not in settings.masked_api_key

    monkeypatch.setenv("GROQ_TIMEOUT", "soon")
    with pytest.raises(ConfigError, match="GROQ_TIMEOUT"):
        load_settings(env)
    monkeypatch.setenv("GROQ_TIMEOUT", "30")
    monkeypatch.setenv("GROQ_REASONING_EFFORT", "extreme")
    with pytest.raises(ConfigError, match="GROQ_REASONING_EFFORT"):
        load_settings(env)


def test_logs_redact_api_keys(tmp_path, capsys):
    secret = "gsk_supersecretvalue123456"
    log_path = configure_logging("INFO", tmp_path, secrets=[secret])
    logging.getLogger("test").error("request failed with key %s", secret, extra={"detail": f"auth {secret}"})
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = log_path.read_text(encoding="utf-8")
    assert secret not in content and secret not in capsys.readouterr().err
    record = json.loads(content.strip().splitlines()[-1])
    assert "REDACTED" in record["message"] and "REDACTED" in record["detail"]
    assert redact("token gsk_ABCDEFGHIJKLMNOP") == "token gsk_***REDACTED***"
    logging.getLogger().handlers.clear()


def test_cli_reports_missing_key(clean_env, monkeypatch, capsys):
    monkeypatch.setattr("src.config.DEFAULT_ENV_FILE", clean_env)
    assert main.main([]) == main.EXIT_CONFIG
    assert "GROQ_API_KEY is not set" in capsys.readouterr().err


def test_cli_requires_booking_for_adhoc_term_sheet(monkeypatch, capsys, tmp_path):
    args = main.parse_args(["--term-sheet", "x.pdf"])
    with pytest.raises(Exception, match="requires --booking"):
        main.build_jobs(args)
