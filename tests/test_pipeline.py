"""Complete workflow on both sample term sheets and all booking records, with mocked LLM responses."""

import csv
import json

import pytest

from src.pipeline import ManifestError, ReconciliationPipeline, load_manifest
from src.schemas import FinalStatus

from .conftest import DATA, FakeLLMClient


@pytest.fixture
def outcome(settings, document_router):
    llm = FakeLLMClient(document_router)
    pipeline = ReconciliationPipeline(settings, llm, booking_format="both", use_cache=False)
    result = pipeline.run(load_manifest(DATA / "manifest.json"))
    result.llm = llm
    return result


def status(outcome, document_prefix, trade_id, field):
    for row in outcome.results:
        if row.document.startswith(document_prefix) and row.trade_id == trade_id and row.field == field:
            return row.final_status
    raise KeyError((document_prefix, trade_id, field))


def test_every_trade_and_field_is_reported(outcome):
    assert len(outcome.results) == 2 * 5 * 19
    assert all(row.final_status is not None for row in outcome.results)
    assert not outcome.job_failures


def test_expected_breaks_are_detected(outcome):
    assert status(outcome, "Genel", "4", "Coupon") == FinalStatus.TRUE_MISMATCH
    assert status(outcome, "Genel", "2", "Maturity") == FinalStatus.TRUE_MISMATCH
    assert status(outcome, "IDBI", "2", "Coupon") == FinalStatus.TRUE_MISMATCH
    assert status(outcome, "IDBI", "4", "IssueDate") == FinalStatus.TRUE_MISMATCH
    assert status(outcome, "IDBI", "3", "Notional") == FinalStatus.TRUE_MISMATCH  # 1.1 bonds
    assert status(outcome, "IDBI", "1", "Maturity") == FinalStatus.TRUE_MISMATCH  # perpetual vs fixed date


def test_clean_fields_match_and_absent_terms_are_unverifiable(outcome):
    assert status(outcome, "Genel", "1", "Coupon") == FinalStatus.MATCH
    assert status(outcome, "Genel", "1", "BusinessDayLocation") == FinalStatus.MATCH
    assert status(outcome, "IDBI", "1", "IssueAmount") == FinalStatus.MATCH  # Rs.1,500 Crores
    assert status(outcome, "IDBI", "1", "InterestPaymentDate") == FinalStatus.MATCH  # derived anniversary
    assert status(outcome, "IDBI", "1", "ISIN") == FinalStatus.UNVERIFIABLE
    assert status(outcome, "Genel", "1", "SettlementDate") == FinalStatus.UNVERIFIABLE
    assert status(outcome, "Genel", "3", "Notional") == FinalStatus.UNVERIFIABLE  # valid multiple of 2,000


def test_semantic_difference_goes_through_second_pass(outcome):
    assert status(outcome, "IDBI", "1", "BusinessDayConvention") == FinalStatus.FALSE_ALERT


def test_reports_are_written_with_required_content(outcome):
    paths = outcome.report_paths
    for key in ("csv", "json", "summary", "markdown"):
        assert paths[key].exists()

    with paths["csv"].open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 190
    row = next(r for r in rows if r["document"].startswith("Genel") and r["trade_id"] == "4" and r["field"] == "Coupon")
    assert row["final_status"] == "True Mismatch" and row["evidence_pages"] == "1"
    assert "9.25% p.a." in row["evidence_quotes"] and row["booking_value"] == "9.5"
    assert row["llm_explanation"] and row["initial_result"] == "MISMATCH"

    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    counts = summary["counts"]
    for key in ("matches", "false_alerts", "true_mismatches", "parsing_errors", "unverifiable_fields",
                "failed_llm_calls"):
        assert key in counts
    assert counts["true_mismatches"] == sum(r.final_status == FinalStatus.TRUE_MISMATCH for r in outcome.results)
    assert {b["parity"]["consistent"] for b in summary["booking"]} == {True}
    trade_status = {(t["document"][:4], t["trade_id"]): t["overall"] for t in summary["by_trade"]}
    assert trade_status[("Gene", "1")] == "CLEAN" and trade_status[("Gene", "4")] == "BREAK"

    detailed = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert len(detailed["results"]) == 190
    report = paths["markdown"].read_text(encoding="utf-8")
    assert "## Exceptions requiring action" in report and "Genel_Energy_Term_Sheet.pdf" in report
    assert (outcome.report_paths["csv"].parent / "extractions" / "IDBI_Omni_Term_Sheet.extraction.json").exists()


def test_llm_usage_is_economical(outcome):
    usage = outcome.summary["llm_usage"]
    assert usage["extraction_calls"] == 2
    assert usage["adjudications_reused"] >= 4  # identical IDBI maturity breaks share one verdict
    assert usage["failed_llm_calls"] == 0


def test_job_failures_do_not_stop_other_jobs(settings, document_router, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"jobs": [
        {"name": "missing", "term_sheet": str(tmp_path / "nope.pdf"), "booking_json": str(DATA / "booking/IDBI_Omni.json")},
        {"name": "genel", "term_sheet": str(DATA / "term_sheets/Genel_Energy_Term_Sheet.pdf"),
         "booking_csv": str(DATA / "booking/Genel_Energy.csv")},
    ]}))
    pipeline = ReconciliationPipeline(settings, FakeLLMClient(document_router), booking_format="csv", use_cache=False)
    result = pipeline.run(load_manifest(manifest))
    assert result.job_failures[0]["stage"] == "document_parsing"
    assert len(result.results) == 5 * 19


def test_manifest_validation(tmp_path):
    bad = tmp_path / "m.json"
    bad.write_text('{"jobs": [{"term_sheet": "x.pdf"}]}')
    with pytest.raises(ManifestError, match="booking"):
        load_manifest(bad)
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(tmp_path / "missing.json")
