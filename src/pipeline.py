"""End-to-end orchestration: parse -> extract -> ingest -> reconcile -> adjudicate -> report.

A failure in one job (unreadable document, invalid booking file, failed
extraction) is recorded and the remaining jobs still run. Only fatal LLM
configuration errors (bad API key, unavailable model) abort the run, because
no further call could succeed.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .booking_loader import BookingDataError, BookingLoadResult, load_booking_records
from .cache import JsonCache
from .config import Settings
from .document_parser import DocumentParseError, parse_document
from .extraction_service import ExtractionService
from .llm_client import LLMClient
from .mismatch_analyzer import MismatchAnalyzer
from .reconciliation import ReconciliationEngine
from .report_generator import build_summary, write_reports
from .schemas import DocumentExtraction, ReconciliationResult

logger = logging.getLogger(__name__)


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class ReconciliationJob:
    name: str
    term_sheet: Path
    booking_csv: Path | None = None
    booking_json: Path | None = None


def load_manifest(path: Path) -> list[ReconciliationJob]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"Manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"Manifest {path.name} is not valid JSON: {exc}") from exc
    jobs_raw = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs_raw, list) or not jobs_raw:
        raise ManifestError(f'Manifest {path.name} must contain a non-empty "jobs" list')
    base = path.parent
    jobs = []
    for index, job in enumerate(jobs_raw, start=1):
        if not isinstance(job, dict) or not job.get("term_sheet"):
            raise ManifestError(f"Manifest job #{index} needs a term_sheet path")
        if not job.get("booking_csv") and not job.get("booking_json"):
            raise ManifestError(f"Manifest job #{index} needs booking_csv and/or booking_json")

        def resolve(key: str) -> Path | None:
            value = job.get(key)
            return (base / value).resolve() if value else None

        jobs.append(ReconciliationJob(
            name=job.get("name") or Path(job["term_sheet"]).stem,
            term_sheet=resolve("term_sheet"),  # type: ignore[arg-type]
            booking_csv=resolve("booking_csv"),
            booking_json=resolve("booking_json"),
        ))
    return jobs


@dataclass
class RunOutcome:
    results: list[ReconciliationResult]
    summary: dict
    report_paths: dict[str, Path]
    extractions: list[DocumentExtraction] = field(default_factory=list)
    job_failures: list[dict] = field(default_factory=list)


class ReconciliationPipeline:
    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        *,
        booking_format: str = "both",
        use_cache: bool = True,
        output_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.booking_format = booking_format
        self.output_dir = output_dir or settings.output_dir
        self.cache = JsonCache(settings.cache_dir, enabled=use_cache)
        self.extractor = ExtractionService(llm, settings, self.cache)
        self.engine = ReconciliationEngine()
        self.analyzer = MismatchAnalyzer(llm, settings, self.cache)

    def run(self, jobs: list[ReconciliationJob]) -> RunOutcome:
        started = datetime.now(timezone.utc)
        results: list[ReconciliationResult] = []
        extractions: list[DocumentExtraction] = []
        booking_info: list[dict] = []
        failures: list[dict] = []

        for job in jobs:
            logger.info("=== Job: %s ===", job.name, extra={"event": "job_start", "job": job.name})
            try:
                document = parse_document(job.term_sheet)
            except DocumentParseError as exc:
                failures.append({"job": job.name, "stage": "document_parsing", "error": str(exc)})
                logger.error("Skipping job %s: %s", job.name, exc)
                continue
            try:
                booking = load_booking_records(
                    job.booking_csv, job.booking_json,
                    booking_format=self.booking_format, date_order=self.settings.booking_date_order,
                )
            except BookingDataError as exc:
                failures.append({"job": job.name, "stage": "booking_ingestion", "error": str(exc)})
                logger.error("Skipping job %s: %s", job.name, exc)
                continue
            booking_info.append(_booking_info(job, booking))

            extraction = self.extractor.extract(document)
            extractions.append(extraction)
            self._save_extraction(extraction)
            if extraction.status != "OK":
                failures.append({"job": job.name, "stage": "extraction", "error": extraction.error})

            job_results = self.engine.reconcile(extraction, booking.trades, booking.source)
            self.analyzer.adjudicate(job_results, extraction)
            results.extend(job_results)

        finished = datetime.now(timezone.utc)
        llm_usage = self._llm_usage()
        run_info = {
            "tool_version": __version__,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_seconds": round((finished - started).total_seconds(), 1),
            "model": self.llm.model,
            "booking_format": self.booking_format,
            "jobs": [job.name for job in jobs],
            "cache_enabled": self.cache.enabled,
        }
        summary = build_summary(results, extractions, run_info, llm_usage, booking_info, failures)
        paths = write_reports(results, summary, extractions, self.output_dir)
        logger.info("Reports written to %s", self.output_dir, extra={"event": "reports_written"})
        return RunOutcome(results, summary, paths, extractions, failures)

    def _save_extraction(self, extraction: DocumentExtraction) -> None:
        directory = self.output_dir / "extractions"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{Path(extraction.document).stem}.extraction.json"
        target.write_text(extraction.model_dump_json(indent=2), encoding="utf-8")

    def _llm_usage(self) -> dict:
        stats = self.llm.stats.as_dict()
        analyzer = self.analyzer.stats
        unusable = self.extractor.invalid_responses + analyzer["invalid_responses"]
        return {
            "model": self.llm.model,
            "extraction_calls": stats["calls"] - analyzer["llm_calls"],
            "adjudication_calls": analyzer["llm_calls"],
            "suspected_mismatches": analyzer["suspected_mismatches"],
            "adjudications_reused": analyzer["reused"],
            "adjudications_from_cache": analyzer["from_cache"],
            "http_requests": stats["http_requests"],
            "retries": stats["retries"],
            "failed_requests": stats["failed"],
            "unusable_responses": unusable,
            "failed_llm_calls": stats["failed"] + unusable,
            "prompt_tokens": stats["prompt_tokens"],
            "completion_tokens": stats["completion_tokens"],
            "throttle_wait_seconds": stats["throttle_wait_seconds"],
            "cache_hits": self.cache.hits,
            "failures": stats["failures"],
        }


def _booking_info(job: ReconciliationJob, booking: BookingLoadResult) -> dict:
    return {
        "job": job.name,
        "source": booking.source,
        "trades": len(booking.trades),
        "trade_ids": [t.trade_id for t in booking.trades],
        "warnings": booking.warnings,
        "parity": booking.parity.as_dict() if booking.parity else None,
    }
