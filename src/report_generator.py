"""Report generation: detailed CSV/JSON, summary JSON and a reviewer-friendly Markdown report."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .normalizer import display_value
from .schemas import DocumentExtraction, ExtractionStatus, FinalStatus, ReconciliationResult

STATUS_ORDER = [s.value for s in FinalStatus]
EXCEPTION_STATUSES = {FinalStatus.TRUE_MISMATCH, FinalStatus.UNRESOLVED, FinalStatus.PARSING_ERROR}

CSV_COLUMNS = [
    "document", "trade_id", "isin", "field", "final_status",
    "term_sheet_value", "booking_value", "booking_normalized", "term_sheet_raw_value",
    "evidence_quotes", "evidence_pages", "evidence_verified", "value_basis", "extraction_note",
    "comparison_method", "initial_result", "initial_reason", "hard_difference", "candidate_equivalence",
    "llm_verdict", "llm_category", "llm_explanation", "llm_evidence", "llm_confidence", "adjudication_source",
    "error_type", "error_details", "field_scope", "booking_source",
]


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " || ".join(str(v) for v in value)
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def trade_overall(statuses: Counter) -> str:
    if statuses[FinalStatus.TRUE_MISMATCH.value]:
        return "BREAK"
    if statuses[FinalStatus.UNRESOLVED.value] or statuses[FinalStatus.PARSING_ERROR.value]:
        return "NEEDS REVIEW"
    return "CLEAN"


def build_summary(
    results: list[ReconciliationResult],
    extractions: list[DocumentExtraction],
    run_info: dict,
    llm_usage: dict,
    booking_info: list[dict],
    job_failures: list[dict],
) -> dict:
    totals = Counter(r.final_status.value for r in results)
    by_document: dict[str, Counter] = defaultdict(Counter)
    by_field: dict[str, Counter] = defaultdict(Counter)
    by_trade: dict[tuple[str, str, str | None], Counter] = defaultdict(Counter)
    for r in results:
        status = r.final_status.value
        by_document[r.document][status] += 1
        by_field[r.field][status] += 1
        by_trade[(r.document, r.trade_id, r.isin)][status] += 1

    def ordered(counter: Counter) -> dict[str, int]:
        return {status: counter.get(status, 0) for status in STATUS_ORDER}

    trades = []
    for (document, trade_id, isin), counter in by_trade.items():
        breaks = [r.field for r in results if r.document == document and r.trade_id == trade_id
                  and r.final_status in EXCEPTION_STATUSES]
        trades.append({"document": document, "trade_id": trade_id, "isin": isin,
                       "overall": trade_overall(counter), "counts": ordered(counter), "exceptions": breaks})

    exceptions = [
        {
            "document": r.document, "trade_id": r.trade_id, "field": r.field, "status": r.final_status.value,
            "term_sheet_value": r.term_sheet_value, "booking_value": r.booking_value,
            "explanation": r.llm_explanation or r.error_details or r.initial_reason,
            "evidence_pages": r.evidence_pages,
        }
        for r in results if r.final_status in EXCEPTION_STATUSES
    ]

    return {
        "run": run_info,
        "counts": {
            "matches": totals.get(FinalStatus.MATCH.value, 0),
            "false_alerts": totals.get(FinalStatus.FALSE_ALERT.value, 0),
            "true_mismatches": totals.get(FinalStatus.TRUE_MISMATCH.value, 0),
            "parsing_errors": totals.get(FinalStatus.PARSING_ERROR.value, 0),
            "unverifiable_fields": totals.get(FinalStatus.UNVERIFIABLE.value, 0),
            "unresolved": totals.get(FinalStatus.UNRESOLVED.value, 0),
            "failed_llm_calls": llm_usage.get("failed_llm_calls", 0),
            "total_field_comparisons": len(results),
        },
        "by_document": {doc: ordered(c) for doc, c in by_document.items()},
        "by_field": {field: ordered(c) for field, c in by_field.items()},
        "by_trade": trades,
        "exceptions": exceptions,
        "extraction": [_extraction_overview(e) for e in extractions],
        "booking": booking_info,
        "job_failures": job_failures,
        "llm_usage": llm_usage,
    }


def _extraction_overview(extraction: DocumentExtraction) -> dict:
    statuses = Counter(f.status.value for f in extraction.fields.values())
    return {
        "document": extraction.document,
        "status": extraction.status,
        "error": extraction.error,
        "model": extraction.model,
        "strategy": extraction.strategy,
        "pages_sent": extraction.pages_sent,
        "chars_sent": extraction.chars_sent,
        "from_cache": extraction.from_cache,
        "llm_calls": extraction.llm_calls,
        "fields_extracted": statuses.get(ExtractionStatus.EXTRACTED.value, 0),
        "fields_not_in_document": [f.field for f in extraction.fields.values()
                                   if f.status == ExtractionStatus.NOT_IN_DOCUMENT],
        "fields_with_parsing_errors": [f.field for f in extraction.fields.values()
                                       if f.status == ExtractionStatus.PARSING_ERROR],
    }


def write_reports(
    results: list[ReconciliationResult],
    summary: dict,
    extractions: list[DocumentExtraction],
    output_dir: Path,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "csv": output_dir / "reconciliation_results.csv",
        "json": output_dir / "reconciliation_results.json",
        "summary": output_dir / "reconciliation_summary.json",
        "markdown": output_dir / "reconciliation_report.md",
    }
    with paths["csv"].open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in results:
            data = row.model_dump()
            writer.writerow({column: _cell(data.get(column)) for column in CSV_COLUMNS})

    paths["json"].write_text(
        json.dumps({"run": summary["run"], "results": [r.model_dump(mode="json") for r in results]},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    paths["summary"].write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    paths["markdown"].write_text(render_markdown(results, summary, extractions), encoding="utf-8")
    return paths


# --------------------------------------------------------------------------- markdown


def _md(text: Any, limit: int = 160) -> str:
    if text is None or text == "":
        return "–"
    value = " || ".join(map(str, text)) if isinstance(text, list) else str(text)
    value = " ".join(value.split()).replace("|", "\\|")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def render_markdown(results: list[ReconciliationResult], summary: dict, extractions: list[DocumentExtraction]) -> str:
    run, counts = summary["run"], summary["counts"]
    lines = [
        "# Term Sheet Reconciliation Report",
        "",
        f"Generated {run.get('finished_at', '')} · model `{run.get('model')}` · booking source: "
        f"{run.get('booking_format')} · {len(summary['by_trade'])} trade(s) across "
        f"{len(summary['by_document'])} term sheet(s)",
        "",
        "## Outcome",
        "",
        "| Match | False Alert | True Mismatch | Parsing Error | Unverifiable | Unresolved | Failed LLM calls |",
        "|---:|---:|---:|---:|---:|---:|---:|",
        f"| {counts['matches']} | {counts['false_alerts']} | {counts['true_mismatches']} | {counts['parsing_errors']} "
        f"| {counts['unverifiable_fields']} | {counts['unresolved']} | {counts['failed_llm_calls']} |",
        "",
        "*Match / False Alert / True Mismatch* are reconciliation outcomes. *Parsing Error* (a value failed "
        "schema or evidence validation), *Unverifiable* (the term sheet does not state the field) and "
        "*Unresolved* (the second LLM pass could not reach a defensible verdict) are exceptions shown separately.",
        "",
        "## Trades",
        "",
        "| Term sheet | Trade | ISIN | Overall | Match | False Alert | True Mismatch | Parsing Error | Unverifiable | Unresolved |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for trade in summary["by_trade"]:
        c = trade["counts"]
        lines.append(
            f"| {_md(trade['document'])} | {trade['trade_id']} | {_md(trade['isin'])} | **{trade['overall']}** "
            f"| {c['Match']} | {c['False Alert']} | {c['True Mismatch']} | {c['Parsing Error']} "
            f"| {c['Unverifiable']} | {c['Unresolved']} |"
        )

    exceptions = [r for r in results if r.final_status in EXCEPTION_STATUSES]
    lines += ["", "## Exceptions requiring action", ""]
    if exceptions:
        lines += [
            "| Term sheet | Trade | Field | Term sheet value (page) | Booking value | Status | Explanation |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in exceptions:
            page = f" (p. {', '.join(map(str, r.evidence_pages))})" if r.evidence_pages else ""
            explanation = r.llm_explanation or r.error_details or r.initial_reason
            if r.error_type == "GUARDRAIL" and r.llm_explanation:
                explanation = f"{r.error_details}. LLM said: {r.llm_explanation}"
            lines.append(
                f"| {_md(r.document)} | {r.trade_id} | {r.field} | {_md(r.term_sheet_value, 60)}{page} "
                f"| {_md(r.booking_value, 60)} | **{r.final_status.value}** | {_md(explanation, 300)} |"
            )
    else:
        lines.append("None.")

    false_alerts = [r for r in results if r.final_status == FinalStatus.FALSE_ALERT]
    lines += ["", "## False alerts (differences explained as non-economic)", ""]
    if false_alerts:
        lines += ["| Term sheet | Trade | Field | Term sheet value | Booking value | Reason |", "|---|---|---|---|---|---|"]
        for r in false_alerts:
            lines.append(f"| {_md(r.document)} | {r.trade_id} | {r.field} | {_md(r.term_sheet_value, 60)} "
                         f"| {_md(r.booking_value, 60)} | {_md(r.llm_explanation, 300)} |")
    else:
        lines.append("None.")

    lines += ["", "## Fields that could not be verified against the term sheet", ""]
    unverifiable: dict[tuple[str, str, str], list[ReconciliationResult]] = defaultdict(list)
    for r in results:
        if r.final_status == FinalStatus.UNVERIFIABLE:
            unverifiable[(r.document, r.field, r.initial_reason)].append(r)
    if unverifiable:
        lines += ["| Term sheet | Field | Trades | Booking value(s) | Reason |", "|---|---|---|---|---|"]
        for (document, field, reason), rows in unverifiable.items():
            booking_values = sorted({r.booking_value or "–" for r in rows})
            lines.append(f"| {_md(document)} | {field} | {', '.join(r.trade_id for r in rows)} "
                         f"| {_md(booking_values, 80)} | {_md(reason, 300)} |")
    else:
        lines.append("None.")

    for extraction in extractions:
        lines += ["", f"## Extracted terms – {extraction.document}", ""]
        if extraction.status != "OK":
            lines.append(f"Extraction failed: {_md(extraction.error, 400)}")
            continue
        lines += [
            f"Strategy: {extraction.strategy}, {extraction.chars_sent} characters from pages {extraction.pages_sent}"
            f"{' (cached)' if extraction.from_cache else ''}.",
            "",
            "| Field | Value | Basis | Page | Evidence | Status |",
            "|---|---|---|---|---|---|",
        ]
        for f in extraction.fields.values():
            value = f.value if f.status == ExtractionStatus.EXTRACTED else None
            status = f.status.value if not f.errors else f"{f.status.value}: {'; '.join(f.errors)}"
            lines.append(
                f"| {f.field} | {_md(display_value(value), 40)} | {_md(f.basis)} "
                f"| {_md(', '.join(map(str, f.pages)) or None)} | {_md([e.quote for e in f.evidence], 140)} "
                f"| {_md(status, 120)} |"
            )

    parity = [b for b in summary.get("booking", []) if b.get("parity")]
    if parity or summary.get("job_failures"):
        lines += ["", "## Data quality", ""]
        for b in parity:
            p = b["parity"]
            state = "consistent" if p["consistent"] else f"{len(p['differences'])} difference(s)"
            lines.append(f"- CSV/JSON parity {p['csv_file']} vs {p['json_file']}: **{state}**")
        for failure in summary.get("job_failures", []):
            lines.append(f"- Job **{failure['job']}** failed: {_md(failure['error'], 300)}")

    usage = summary.get("llm_usage", {})
    lines += [
        "",
        "## LLM usage",
        "",
        f"- Extraction calls: {usage.get('extraction_calls', 0)}; adjudication calls: "
        f"{usage.get('adjudication_calls', 0)} (reused verdicts: {usage.get('adjudications_reused', 0)}, "
        f"from cache: {usage.get('adjudications_from_cache', 0)})",
        f"- Failed or unusable LLM calls: {usage.get('failed_llm_calls', 0)}; retries: {usage.get('retries', 0)}; "
        f"tokens in/out: {usage.get('prompt_tokens', 0)}/{usage.get('completion_tokens', 0)}",
        "",
    ]
    return "\n".join(lines)
