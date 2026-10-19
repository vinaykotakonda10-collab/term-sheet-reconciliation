import json
from datetime import date
from decimal import Decimal

import pytest

from src.booking_loader import load_booking_file
from src.cache import JsonCache
from src.llm_client import LLMAuthenticationError, LLMRateLimitError
from src.mismatch_analyzer import MismatchAnalyzer, build_payload
from src.reconciliation import ReconciliationEngine
from src.schemas import FinalStatus

from .conftest import GENEL_JSON, IDBI_JSON, FakeLLMClient, generic_adjudicator
from .test_reconciliation import GENEL_TERMS, make_extraction, rows_by


def verdict(v="TRUE_MISMATCH", category="VALUE_DIFFERENCE", confidence="high", evidence=("9.25% p.a.",)):
    return json.dumps({"verdict": v, "category": category, "explanation": "Reviewed against the quote.",
                       "supporting_evidence": list(evidence), "confidence": confidence})


def run(extraction, trades, responder, settings, cache=None):
    llm = FakeLLMClient(lambda purpose, system, user: responder(user))
    results = ReconciliationEngine().reconcile(extraction, trades, "x")
    analyzer = MismatchAnalyzer(llm, settings, cache)
    analyzer.adjudicate(results, extraction)
    return results, analyzer, llm


def test_true_mismatch_and_reasoning_are_recorded(settings):
    results, analyzer, llm = run(make_extraction(GENEL_TERMS), load_booking_file(GENEL_JSON).trades,
                                 generic_adjudicator, settings)
    coupon = rows_by(results, "4")["Coupon"]
    assert coupon.final_status == FinalStatus.TRUE_MISMATCH
    assert coupon.llm_verdict == "TRUE_MISMATCH" and coupon.llm_explanation and coupon.llm_evidence
    assert coupon.initial_reason.startswith("Rates differ")  # original comparison retained
    assert rows_by(results, "2")["Maturity"].final_status == FinalStatus.TRUE_MISMATCH
    assert analyzer.stats["suspected_mismatches"] == 2 and len(llm.calls) == 2


def test_false_alert_for_decimal_rate(settings):
    trades = load_booking_file(GENEL_JSON).trades
    trades[0].values["Coupon"].value = Decimal("0.0925")
    trades[0].values["Coupon"].raw = 0.0925
    results, _, _ = run(make_extraction(GENEL_TERMS), trades[:1],
                        lambda user: verdict("FALSE_ALERT", "UNIT_OR_SCALE_DIFFERENCE"), settings)
    row = rows_by(results, "1")["Coupon"]
    assert row.final_status == FinalStatus.FALSE_ALERT and row.candidate_equivalence == "decimal_rate_vs_percent"


def test_guardrail_blocks_false_alert_on_numeric_difference(settings):
    trades = load_booking_file(GENEL_JSON).trades
    results, _, _ = run(make_extraction(GENEL_TERMS), trades, lambda user: verdict("FALSE_ALERT", "OTHER"), settings)
    row = rows_by(results, "4")["Coupon"]
    assert row.llm_verdict == "FALSE_ALERT"
    assert row.final_status == FinalStatus.UNRESOLVED and row.error_type == "GUARDRAIL"


def test_low_confidence_or_unsupported_verdicts_are_unresolved(settings):
    trades = load_booking_file(IDBI_JSON).trades[:1]
    extraction = make_extraction({"BusinessDayConvention": "Following for coupons; Preceding otherwise"})
    low, _, _ = run(extraction, trades, lambda user: verdict("FALSE_ALERT", confidence="low"), settings)
    assert rows_by(low, "1")["BusinessDayConvention"].final_status == FinalStatus.UNRESOLVED
    bare, _, _ = run(extraction, trades, lambda user: verdict("FALSE_ALERT", evidence=()), settings)
    assert rows_by(bare, "1")["BusinessDayConvention"].final_status == FinalStatus.UNRESOLVED
    unsure, _, _ = run(extraction, trades, lambda user: verdict("INSUFFICIENT_EVIDENCE", "OTHER"), settings)
    assert rows_by(unsure, "1")["BusinessDayConvention"].final_status == FinalStatus.UNRESOLVED
    ok, _, _ = run(extraction, trades, lambda user: verdict("FALSE_ALERT", "EQUIVALENT_TERMINOLOGY"), settings)
    assert rows_by(ok, "1")["BusinessDayConvention"].final_status == FinalStatus.FALSE_ALERT


def test_weekend_business_day_roll_is_the_only_hard_date_exception(settings):
    terms = dict(GENEL_TERMS, Maturity=date(2025, 10, 11))  # a Saturday
    trades = load_booking_file(GENEL_JSON).trades[:1]
    trades[0].values["Maturity"].value = date(2025, 10, 13)  # following Monday
    results, _, _ = run(make_extraction(terms), trades,
                        lambda user: verdict("FALSE_ALERT", "BUSINESS_DAY_ADJUSTMENT"), settings)
    assert rows_by(results, "1")["Maturity"].final_status == FinalStatus.FALSE_ALERT


def test_invalid_json_gets_one_repair_then_unresolved(settings):
    answers = iter(["not json", "{\"verdict\": \"PERHAPS\"}"])
    trades = load_booking_file(GENEL_JSON).trades
    results, analyzer, llm = run(make_extraction(GENEL_TERMS), trades[3:4], lambda user: next(answers), settings)
    row = rows_by(results, "4")["Coupon"]
    assert row.final_status == FinalStatus.UNRESOLVED and row.error_type == "LLM_INVALID_RESPONSE"
    assert analyzer.stats["invalid_responses"] == 2 and len(llm.calls) == 2


def test_api_failure_is_unresolved_and_counted(settings):
    def fail(user):
        raise LLMRateLimitError("Groq rate limit hit (HTTP 429)")

    results, analyzer, _ = run(make_extraction(GENEL_TERMS), load_booking_file(GENEL_JSON).trades[3:4], fail,
                               settings)
    row = rows_by(results, "4")["Coupon"]
    assert row.final_status == FinalStatus.UNRESOLVED and row.error_type == "LLM_RATE_LIMIT"
    assert analyzer.stats["failed"] == 1


def test_fatal_errors_propagate(settings):
    def fail(user):
        raise LLMAuthenticationError("bad key")

    with pytest.raises(LLMAuthenticationError):
        run(make_extraction(GENEL_TERMS), load_booking_file(GENEL_JSON).trades[3:4], fail, settings)


def test_identical_mismatches_are_adjudicated_once(settings):
    trades = load_booking_file(IDBI_JSON).trades
    results, analyzer, llm = run(make_extraction({"Maturity": "PERPETUAL"}), trades, generic_adjudicator, settings)
    maturity = [r for r in results if r.field == "Maturity"]
    assert all(r.final_status == FinalStatus.TRUE_MISMATCH for r in maturity)
    assert [r.adjudication_source for r in maturity].count("llm") == 1
    assert analyzer.stats["reused"] == 4
    assert len([c for c in llm.calls if "Maturity" in c["user"]]) == 1


def test_adjudication_cache_avoids_repeat_calls(settings, tmp_path):
    cache = JsonCache(tmp_path / "cache")
    trades = load_booking_file(GENEL_JSON).trades[3:4]
    run(make_extraction(GENEL_TERMS), trades, generic_adjudicator, settings, cache)
    results, analyzer, llm = run(make_extraction(GENEL_TERMS), trades, generic_adjudicator, settings, cache)
    assert len(llm.calls) == 0 and analyzer.stats["from_cache"] == 1
    assert rows_by(results, "4")["Coupon"].adjudication_source == "cache"


def test_payload_contains_context_but_no_trade_identity():
    trades = load_booking_file(IDBI_JSON).trades
    extraction = make_extraction({"NominalAmountPerBond": Decimal("1000000"), "MinimumSubscription": Decimal("5000000")})
    row = rows_by(ReconciliationEngine().reconcile(extraction, trades, "x"), "3")["Notional"]
    payload = build_payload(row, extraction)
    assert payload["related_terms"]["NominalAmountPerBond"]["value"] == "1000000"
    assert payload["booking"]["raw_value"] == "1100000"
    assert "trade_id" not in json.dumps(payload)
