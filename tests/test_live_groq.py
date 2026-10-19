"""Live end-to-end checks against the real Groq API (opt-in).

Run with:  RUN_LIVE_TESTS=1 pytest -m live
These use the free-tier quota and take a few minutes because of token-per-minute limits.
"""

import os
from datetime import date
from decimal import Decimal

import pytest

from src.config import ConfigError, load_settings
from src.document_parser import parse_document
from src.extraction_service import ExtractionService
from src.llm_client import GroqLLMClient
from src.schemas import ExtractionStatus

from .conftest import GENEL_PDF, IDBI_PDF

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live_service():
    if os.getenv("RUN_LIVE_TESTS") != "1":
        pytest.skip("set RUN_LIVE_TESTS=1 to call the real Groq API")
    try:
        settings = load_settings()
    except ConfigError as exc:
        pytest.skip(str(exc))
    return ExtractionService(GroqLLMClient(settings), settings, cache=None)


def test_live_genel_extraction(live_service):
    fields = live_service.extract(parse_document(GENEL_PDF)).fields
    assert fields["ISIN"].value == "NO0010894330"
    assert fields["Coupon"].value == Decimal("9.25")
    assert fields["Maturity"].value == date(2025, 10, 14)
    assert fields["Currency"].value == "USD"


def test_live_idbi_extraction(live_service):
    fields = live_service.extract(parse_document(IDBI_PDF)).fields
    assert fields["Maturity"].value == "PERPETUAL"
    assert fields["Coupon"].value == Decimal("10.75")
    assert fields["ISIN"].status == ExtractionStatus.NOT_IN_DOCUMENT
