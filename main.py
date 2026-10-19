"""Command-line entry point for the term sheet reconciliation workflow.

Examples
    python main.py                                   # all jobs in data/manifest.json, CSV+JSON with parity check
    python main.py --booking-format csv              # reconcile from the CSV extracts only
    python main.py --term-sheet path/to/ts.pdf --booking-json path/to/trades.json
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from src.config import PROJECT_ROOT, ConfigError, load_settings
from src.llm_client import GroqLLMClient, LLMError
from src.logging_setup import configure_logging
from src.pipeline import ManifestError, ReconciliationJob, ReconciliationPipeline, load_manifest

EXIT_OK, EXIT_PARTIAL, EXIT_CONFIG, EXIT_LLM = 0, 1, 2, 3


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LLM-powered term sheet reconciliation (Groq Cloud).")
    parser.add_argument("--manifest", type=Path, default=PROJECT_ROOT / "data" / "manifest.json",
                        help="JSON manifest listing term sheets and their booking files (default: data/manifest.json)")
    parser.add_argument("--term-sheet", type=Path, help="Run a single ad-hoc job for this PDF/DOCX term sheet")
    parser.add_argument("--booking-csv", type=Path, help="Booking CSV for --term-sheet")
    parser.add_argument("--booking-json", type=Path, help="Booking JSON for --term-sheet")
    parser.add_argument("--booking-format", choices=("both", "csv", "json"), default="both",
                        help="Which booking extract to reconcile; 'both' reconciles once and checks CSV/JSON parity")
    parser.add_argument("--date-order", choices=("auto", "MDY", "DMY"),
                        help="Day/month order for numeric booking dates (default: BOOKING_DATE_ORDER or auto)")
    parser.add_argument("--output-dir", type=Path, help="Directory for reports (default: outputs/)")
    parser.add_argument("--model", help="Override GROQ_MODEL for this run")
    parser.add_argument("--no-cache", action="store_true", help="Ignore cached LLM responses and call the API")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"), help="Console log level")
    return parser.parse_args(argv)


def build_jobs(args: argparse.Namespace) -> list[ReconciliationJob]:
    if args.term_sheet:
        if not (args.booking_csv or args.booking_json):
            raise ManifestError("--term-sheet requires --booking-csv and/or --booking-json")
        return [ReconciliationJob(args.term_sheet.stem, args.term_sheet, args.booking_csv, args.booking_json)]
    return load_manifest(args.manifest)


def print_summary(summary: dict, paths: dict[str, Path]) -> None:
    c = summary["counts"]
    print("\n" + "=" * 72)
    print("RECONCILIATION SUMMARY")
    print("=" * 72)
    print(f"  Match            {c['matches']:>5}      Parsing Error    {c['parsing_errors']:>5}")
    print(f"  False Alert      {c['false_alerts']:>5}      Unverifiable     {c['unverifiable_fields']:>5}")
    print(f"  True Mismatch    {c['true_mismatches']:>5}      Unresolved       {c['unresolved']:>5}")
    print(f"  Failed LLM calls {c['failed_llm_calls']:>5}      Field comparisons {c['total_field_comparisons']:>4}")
    print("-" * 72)
    for trade in summary["by_trade"]:
        flagged = ", ".join(trade["exceptions"]) or "-"
        print(f"  {trade['document'][:30]:<30} trade {trade['trade_id']:<4} {trade['overall']:<13} {flagged}")
    for failure in summary.get("job_failures", []):
        print(f"  JOB FAILED: {failure['job']} ({failure['stage']}): {failure['error']}")
    print("-" * 72)
    for label, path in paths.items():
        print(f"  {label:<9} {path}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    overrides = {}
    if args.model:
        overrides["groq_model"] = args.model
    if args.date_order:
        overrides["booking_date_order"] = args.date_order
    if args.output_dir:
        overrides["output_dir"] = args.output_dir.resolve()
    if args.log_level:
        overrides["log_level"] = args.log_level
    if overrides:
        from dataclasses import replace

        settings = replace(settings, **overrides)

    log_path = configure_logging(settings.log_level, settings.log_dir, secrets=[settings.groq_api_key])
    logger = logging.getLogger("main")
    logger.info("Model %s, API key %s, log file %s", settings.groq_model, settings.masked_api_key, log_path)

    try:
        jobs = build_jobs(args)
    except ManifestError as exc:
        logger.error("%s", exc)
        return EXIT_CONFIG

    pipeline = ReconciliationPipeline(
        settings, GroqLLMClient(settings), booking_format=args.booking_format, use_cache=not args.no_cache,
    )
    try:
        outcome = pipeline.run(jobs)
    except LLMError as exc:
        logger.error("Aborting: %s [%s]", exc, exc.category)
        print(f"\nLLM configuration problem ({exc.category}): {exc}\n"
              "Run `python test_groq_connection.py` to diagnose the API key and model.", file=sys.stderr)
        return EXIT_LLM

    print_summary(outcome.summary, outcome.report_paths)
    return EXIT_PARTIAL if outcome.job_failures else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
