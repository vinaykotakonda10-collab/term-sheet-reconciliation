"""Shared fixtures: sample data paths, settings, a scriptable fake LLM and realistic extraction payloads.

The extraction payloads below quote the real sample term sheets verbatim, so
tests exercise genuine evidence verification rather than a stub.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from pathlib import Path

import pytest

from src.config import Settings
from src.document_parser import parse_document
from src.field_catalog import FIELD_NAMES
from src.llm_client import LLMResponse, LLMStats

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IDBI_PDF = DATA / "term_sheets" / "IDBI_Omni_Term_Sheet.pdf"
GENEL_PDF = DATA / "term_sheets" / "Genel_Energy_Term_Sheet.pdf"
IDBI_CSV, IDBI_JSON = DATA / "booking" / "IDBI_Omni.csv", DATA / "booking" / "IDBI_Omni.json"
GENEL_CSV, GENEL_JSON = DATA / "booking" / "Genel_Energy.csv", DATA / "booking" / "Genel_Energy.json"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        groq_api_key="gsk_test_key_not_real_0123456789",
        groq_model="fake-model",
        groq_max_retries=2,
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "outputs",
        log_dir=tmp_path / "logs",
    )


@pytest.fixture(scope="session")
def idbi_document():
    return parse_document(IDBI_PDF)


@pytest.fixture(scope="session")
def genel_document():
    return parse_document(GENEL_PDF)


class FakeLLMClient:
    """LLMClient test double; ``responder(purpose, system, user)`` returns content or raises."""

    def __init__(self, responder: Callable[[str, str, str], str], model: str = "fake-model") -> None:
        self.model = model
        self.stats = LLMStats()
        self.calls: list[dict] = []
        self._responder = responder

    def complete_json(self, *, system: str, user: str, purpose: str, max_tokens: int) -> LLMResponse:
        self.calls.append({"purpose": purpose, "system": system, "user": user})
        self.stats.calls += 1
        try:
            content = self._responder(purpose, system, user)
        except Exception:
            self.stats.failed += 1
            raise
        self.stats.succeeded += 1
        return LLMResponse(content=content, model=self.model, purpose=purpose, attempts=1, latency_seconds=0.0)

    def calls_for(self, purpose: str) -> list[dict]:
        return [c for c in self.calls if c["purpose"] == purpose]


def _field(value, quotes=(), basis=None, raw=None, note=None):
    return {
        "value": value,
        "raw_value": raw,
        "evidence": [{"quote": q, "page": p} for q, p in quotes],
        "basis": basis if value is not None else None,
        "note": note,
    }


def genel_fields() -> dict:
    return {
        "ISIN": _field("NO0010894330", [("ISIN: NO 0010894330", 1)], "stated", "NO 0010894330"),
        "Issuer": _field("Genel Energy Finance 4 Limited",
                         [("Genel Energy Finance 4 Limited (incorporated in England", 1)], "stated"),
        "Parent": _field("Genel Energy plc",
                         [("Genel Energy plc (incorporated in Jersey, company registration number 107897)", 1)],
                         "stated"),
        "Maturity": _field("2025-10-14", [("Expected to be 14 October 2025 (5 years after Issue Date).", 1)],
                           "stated", "14 October 2025", note="Stated as expected date"),
        "Notional": _field(None, note="Issue-level term sheet; no trade notional"),
        "Coupon": _field(9.25, [("9.25% p.a., semi-annual interest payments.", 1)], "stated", "9.25% p.a."),
        "Currency": _field("USD", [("Currency: USD", 1)], "stated", "USD"),
        "SettlementDate": _field(None, note="No settlement date stated"),
        "IssueDate": _field("2020-10-14", [("Issue Date: Expected to be 14 October 2020", 1)], "stated"),
        "IssueAmount": _field(300000000, [("Issue Amount: USD 300,000,000", 1)], "stated", "USD 300,000,000"),
        "IssuePrice": _field(97, [("97% of the Nominal Amount.", 1)], "stated", "97%"),
        "NominalAmountPerBond": _field(2000, [("The Bonds will have a nominal amount of USD 2,000 each.", 1)],
                                       "stated", "USD 2,000"),
        "InterestPaymentDate": _field("2021-04-14", [("Expected to be 14 April 2021 (6 months after Issue Date).", 1)],
                                      "stated", "14 April 2021"),
        "InterestPaymentFrequency": _field("Semi-annual", [("9.25% p.a., semi-annual interest payments.", 1)],
                                           "stated", "semi-annual"),
        "DayCountFraction": _field("30/360", [('Day-count fraction for coupon is "30/360"', 1)], "stated", "30/360"),
        "BusinessDayConvention": _field("Unadjusted", [('business day convention is "unadjusted"', 1)], "stated",
                                        "unadjusted"),
        "BusinessDayLocation": _field(["Oslo", "New York"], [('business day is "Oslo" and "New York"', 1)], "stated"),
        "AmortizationType": _field(
            "Bullet",
            [("The Bonds shall be repaid in full at the Maturity Date at 100% of the Nominal Amount", 1)], "stated"),
        "MinimumSubscription": _field(200000,
                                      [("Minimum subscription and allotment amount shall be USD 200,000", 2)],
                                      "stated", "USD 200,000"),
    }


def idbi_fields() -> dict:
    return {
        "ISIN": _field(None, note="No ISIN in the term sheet"),
        "Issuer": _field("IDBI Bank Limited", [("Issuer IDBI Bank Limited", 1)], "stated"),
        "Parent": _field(None),
        "Maturity": _field("PERPETUAL", [("Tenor Perpetual", 3)], "stated", "Perpetual"),
        "Notional": _field(None),
        "Coupon": _field(10.75, [("Coupon Rate 10.75% p.a.", 3)], "stated", "10.75% p.a."),
        "Currency": _field("INR", [("Rs.10,00,000/- (Rupees Ten Lakh) per Bond.", 3)], "derived", "Rs.",
                           note="Amounts are stated in Rupees"),
        "SettlementDate": _field(None, note="Only a pay-in window is given"),
        "IssueDate": _field("2014-10-17", [("Deemed Date of Allotment October 17, 2014", 8)], "stated",
                            "October 17, 2014"),
        "IssueAmount": _field(15000000000,
                              [("Rs.1,500 Crores with an option to retain over subscription up to Rs.1,000 Crores.", 1)],
                              "stated", "Rs.1,500 Crores", note="Green-shoe option of Rs.1,000 Crores"),
        "IssuePrice": _field(None),
        "NominalAmountPerBond": _field(1000000, [("Rs.10,00,000/- (Rupees Ten Lakh) per Bond.", 3)], "stated",
                                       "Rs.10,00,000/-"),
        "InterestPaymentDate": _field("2015-10-17", [("On the anniversary of the Deemed Date of Allotment.", 3),
                                                     ("October 17, 2014", 8)], "derived",
                                      note="First anniversary of the Deemed Date of Allotment (17 Oct 2014)"),
        "InterestPaymentFrequency": _field("Annual", [("coupon will be payable annually in arrear", 3)], "stated"),
        "DayCountFraction": _field("Actual/Actual", [("Computation of Interest Actual/ Actual", 4)], "stated"),
        "BusinessDayConvention": _field(
            "Following for Coupon Payment Dates; Preceding for other dates",
            [("fall on day which is not a business day, the immediately next business day shall be considered as "
              "the effective date.", 15)], "stated"),
        "BusinessDayLocation": _field(None),
        "AmortizationType": _field("Perpetual", [("Tenor Perpetual", 3)], "stated"),
        "MinimumSubscription": _field(5000000, [("5 Bonds and in multiples of 1 Bond thereafter", 8)], "derived",
                                      note="5 bonds x Rs.10,00,000 face value"),
    }


def extraction_json(fields: dict, **overrides) -> str:
    payload = copy.deepcopy(fields)
    for name, value in overrides.items():
        payload[name] = value
    assert set(payload) == set(FIELD_NAMES)
    return json.dumps({"fields": payload})


def generic_adjudicator(user: str) -> str:
    """Mock second pass: decides only from the deterministic facts in the payload (no sample-specific logic)."""
    body = user.split("<booking_comparison>", 1)[1].rsplit("</booking_comparison>", 1)[0]
    payload = json.loads(body)
    check = payload["deterministic_check"]
    if check["values_differ_in_same_unit"]:
        verdict, category = "TRUE_MISMATCH", "VALUE_DIFFERENCE"
    else:
        verdict, category = "FALSE_ALERT", "EQUIVALENT_TERMINOLOGY"
    return json.dumps({
        "verdict": verdict,
        "category": category,
        "explanation": f"Mock decision for {payload['field']}: {check['finding']}",
        "supporting_evidence": [str(payload["booking"]["raw_value"])],
        "confidence": "high",
    })


@pytest.fixture
def document_router():
    """Responder that serves the right extraction payload per document and mock adjudications."""

    def respond(purpose: str, system: str, user: str) -> str:
        if purpose.startswith("extraction"):
            if "IDBI" in user:
                return extraction_json(idbi_fields())
            return extraction_json(genel_fields())
        return generic_adjudicator(user)

    return respond
