"""Deterministic comparison rules, exercised through the engine with real sample bookings."""

from datetime import date
from decimal import Decimal

import pytest

from src.booking_loader import load_booking_file
from src.field_catalog import SPEC_BY_NAME, FieldKind
from src.reconciliation import ComparisonContext, ReconciliationEngine, compare_percent
from src.schemas import (
    BookingTrade,
    BookingValue,
    DocumentExtraction,
    ExtractionStatus,
    FieldExtraction,
    FinalStatus,
    InitialResult,
    VerifiedEvidence,
)

from .conftest import GENEL_JSON, IDBI_JSON


def make_extraction(values: dict, *, errors: dict | None = None) -> DocumentExtraction:
    fields = {}
    for name in SPEC_BY_NAME:
        if errors and name in errors:
            fields[name] = FieldExtraction(field=name, status=ExtractionStatus.PARSING_ERROR,
                                           error_type="SCHEMA_VALIDATION", errors=[errors[name]])
        elif values.get(name) is not None:
            fields[name] = FieldExtraction(
                field=name, status=ExtractionStatus.EXTRACTED, value=values[name], basis="stated",
                evidence=[VerifiedEvidence(quote=f"quote for {name}", page=1, verified=True)],
            )
        else:
            fields[name] = FieldExtraction(field=name, status=ExtractionStatus.NOT_IN_DOCUMENT)
    return DocumentExtraction(document="ts.pdf", document_path="ts.pdf", sha256="0" * 64, file_type="pdf",
                              page_count=1, model="m", prompt_version="p", extracted_at="now", status="OK",
                              fields=fields)


GENEL_TERMS = {
    "ISIN": "NO0010894330", "Issuer": "Genel Energy Finance 4 Limited", "Parent": "Genel Energy plc",
    "Maturity": date(2025, 10, 14), "Coupon": Decimal("9.25"), "Currency": "USD",
    "IssueDate": date(2020, 10, 14), "IssueAmount": Decimal("300000000"), "IssuePrice": Decimal("97"),
    "NominalAmountPerBond": Decimal("2000"), "InterestPaymentDate": date(2021, 4, 14),
    "InterestPaymentFrequency": "Semi-annual", "DayCountFraction": "30/360", "BusinessDayConvention": "Unadjusted",
    "BusinessDayLocation": ["New York", "Oslo"], "AmortizationType": "Bullet",
    "MinimumSubscription": Decimal("200000"),
}


def rows_by(results, trade_id):
    return {r.field: r for r in results if r.trade_id == trade_id}


@pytest.fixture(scope="module")
def genel_results():
    trades = load_booking_file(GENEL_JSON).trades
    return ReconciliationEngine().reconcile(make_extraction(GENEL_TERMS), trades, "Genel_Energy.json")


def test_exact_matches_with_format_normalisation(genel_results):
    clean = rows_by(genel_results, "1")
    for name in ("ISIN", "Issuer", "Parent", "Maturity", "Coupon", "Currency", "IssueDate", "IssueAmount",
                 "IssuePrice", "NominalAmountPerBond", "InterestPaymentDate", "InterestPaymentFrequency",
                 "DayCountFraction", "BusinessDayConvention", "BusinessDayLocation", "AmortizationType",
                 "MinimumSubscription"):
        assert clean[name].final_status == FinalStatus.MATCH, (name, clean[name].initial_reason)
    assert clean["BusinessDayLocation"].initial_reason.startswith("Same set")  # order ignored


def test_missing_term_sheet_field_is_unverifiable_not_matched(genel_results):
    row = rows_by(genel_results, "1")["SettlementDate"]
    assert row.initial_result == InitialResult.NOT_COMPARABLE
    assert row.final_status == FinalStatus.UNVERIFIABLE
    assert row.booking_value == "2020-10-14"


def test_true_mismatches_are_hard_differences(genel_results):
    maturity = rows_by(genel_results, "2")["Maturity"]
    assert maturity.initial_result == InitialResult.MISMATCH and maturity.hard_difference
    assert "+1 day" in maturity.initial_reason
    coupon = rows_by(genel_results, "4")["Coupon"]
    assert coupon.initial_result == InitialResult.MISMATCH and coupon.hard_difference
    assert coupon.final_status is None  # awaits the second LLM pass


def test_notional_is_never_compared_with_issue_size(genel_results):
    for trade_id in ("1", "3"):
        row = rows_by(genel_results, trade_id)["Notional"]
        assert row.final_status == FinalStatus.UNVERIFIABLE
        assert "issue size is a different quantity" in row.initial_reason
        assert "300000000" not in (row.term_sheet_value or "")
    assert "1550 bond(s)" in rows_by(genel_results, "3")["Notional"].initial_reason


def test_notional_not_a_whole_number_of_bonds_is_flagged():
    trades = load_booking_file(IDBI_JSON).trades
    extraction = make_extraction({"NominalAmountPerBond": Decimal("1000000"), "Maturity": "PERPETUAL"})
    results = ReconciliationEngine().reconcile(extraction, trades, "IDBI_Omni.json")
    row = rows_by(results, "3")["Notional"]
    assert row.initial_result == InitialResult.MISMATCH and row.hard_difference
    assert "1.1" in row.initial_reason
    assert row.evidence_quotes == ["[NominalAmountPerBond] quote for NominalAmountPerBond"]
    assert rows_by(results, "1")["Notional"].final_status == FinalStatus.UNVERIFIABLE


def test_perpetual_versus_fixed_maturity_is_flagged():
    trades = load_booking_file(IDBI_JSON).trades
    results = ReconciliationEngine().reconcile(make_extraction({"Maturity": "PERPETUAL"}), trades, "x")
    row = rows_by(results, "1")["Maturity"]
    assert row.initial_result == InitialResult.MISMATCH and row.hard_difference
    assert "perpetual" in row.initial_reason.lower()
    assert row.term_sheet_value == "PERPETUAL" and row.booking_normalized == "2025-10-17"


def _ctx():
    return ComparisonContext(make_extraction({}), BookingTrade(trade_id="1", isin=None, source_file="f",
                                                               source_format="json", record_number=1, values={}))


def test_percent_representation_differences_are_candidates_for_false_alert():
    spec = SPEC_BY_NAME["Coupon"]
    decimal_rate = compare_percent(spec, Decimal("10.75"), Decimal("0.1075"), _ctx())
    assert decimal_rate.result == InitialResult.MISMATCH and not decimal_rate.hard_difference
    assert decimal_rate.candidate_equivalence == "decimal_rate_vs_percent"
    assert compare_percent(spec, Decimal("9.25"), Decimal("9.250"), _ctx()).result == InitialResult.MATCH
    real = compare_percent(spec, Decimal("9.25"), Decimal("9.50"), _ctx())
    assert real.hard_difference and "0.25 percentage points" in real.reason


def test_unit_scale_amount_difference_is_soft():
    trades = load_booking_file(IDBI_JSON).trades
    results = ReconciliationEngine().reconcile(make_extraction({"IssueAmount": Decimal("1500")}), trades, "x")
    row = rows_by(results, "1")["IssueAmount"]
    assert row.initial_result == InitialResult.MISMATCH and not row.hard_difference
    assert "crore" in row.candidate_equivalence


def test_non_standard_convention_text_needs_semantic_review():
    trades = load_booking_file(IDBI_JSON).trades
    extraction = make_extraction({"BusinessDayConvention": "Following for coupon dates; Preceding otherwise"})
    row = rows_by(ReconciliationEngine().reconcile(extraction, trades, "x"), "1")["BusinessDayConvention"]
    assert row.initial_result == InitialResult.MISMATCH and not row.hard_difference


def test_parsing_errors_are_not_compared():
    trades = load_booking_file(GENEL_JSON).trades
    extraction = make_extraction(GENEL_TERMS, errors={"Coupon": "value: must be a JSON number, got str '9.25%'"})
    row = rows_by(ReconciliationEngine().reconcile(extraction, trades, "x"), "4")["Coupon"]
    assert row.final_status == FinalStatus.PARSING_ERROR and row.initial_result == InitialResult.PARSING_ERROR
    assert "must be a JSON number" in row.error_details


def test_invalid_booking_value_is_a_parsing_error():
    trade = BookingTrade(trade_id="9", isin="NO0010894330", source_file="f", source_format="csv", record_number=1,
                         values={"Coupon": BookingValue(raw="abc", error="not a number: 'abc'")})
    results = ReconciliationEngine().reconcile(make_extraction(GENEL_TERMS), [trade], "x")
    row = rows_by(results, "9")["Coupon"]
    assert row.final_status == FinalStatus.PARSING_ERROR and row.error_type == "BOOKING_VALUE_INVALID"
    assert rows_by(results, "9")["Issuer"].initial_result == InitialResult.MISMATCH  # booking value missing


def test_failed_extraction_marks_every_field_as_parsing_error():
    extraction = make_extraction({})
    extraction.status, extraction.error = "FAILED", "RATE_LIMIT: quota"
    results = ReconciliationEngine().reconcile(extraction, load_booking_file(GENEL_JSON).trades, "x")
    assert {r.final_status for r in results} == {FinalStatus.PARSING_ERROR}
    assert all(r.error_type == "EXTRACTION_FAILED" for r in results)


def test_engine_is_extendable():
    engine = ReconciliationEngine()
    engine.register_kind(FieldKind.TEXT, lambda spec, ts, bk, ctx: compare_percent(
        SPEC_BY_NAME["Coupon"], Decimal(1), Decimal(1), ctx))
    results = engine.reconcile(make_extraction({"Issuer": "Somebody Else"}), load_booking_file(GENEL_JSON).trades, "x")
    assert rows_by(results, "1")["Issuer"].final_status == FinalStatus.MATCH
