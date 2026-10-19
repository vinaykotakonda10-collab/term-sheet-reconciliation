"""Second LLM pass: classify each suspected mismatch as True Mismatch or False Alert.

For every row whose initial result is ``MISMATCH`` the analyzer sends the field
definition, the term-sheet value with its verified quotes, the raw and
normalised booking values, the deterministic finding and related terms (e.g.
the denomination when judging a notional). The response is validated with
``AdjudicationResponse``.

Guardrails keep the LLM from waving away real breaks:

* ``INSUFFICIENT_EVIDENCE``, a verdict without supporting evidence, a failed
  call or an invalid response -> **Unresolved** (never a fabricated decision);
* a ``FALSE_ALERT`` for values that differ *in the same unit after
  normalisation* is held as **Unresolved** unless it is a weekend business-day
  roll the engine can verify; a low-confidence ``FALSE_ALERT`` is also held.

Identical comparisons (same field, values, evidence – e.g. the same wrong
maturity on five trades) are adjudicated once and the verdict is reused, which
saves free-tier quota; the report marks such rows ``llm_reused``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from .cache import JsonCache
from .config import Settings
from .field_catalog import SPEC_BY_NAME, FieldKind
from .llm_client import LLMClient, LLMError, LLMResponseFormatError, parse_json_object
from .normalizer import display_value
from .prompts import ADJUDICATION_PROMPT_VERSION, ADJUDICATION_SYSTEM_PROMPT, build_adjudication_user_prompt
from .schemas import (
    AdjudicationResponse,
    DocumentExtraction,
    ExtractionStatus,
    FinalStatus,
    InitialResult,
    ReconciliationResult,
)

logger = logging.getLogger(__name__)

CACHE_NAMESPACE = "adjudications"

RELATED_TERMS: dict[str, tuple[str, ...]] = {
    "Maturity": ("AmortizationType", "IssueDate", "BusinessDayConvention"),
    "Notional": ("NominalAmountPerBond", "MinimumSubscription", "IssueAmount", "Currency"),
    "Coupon": ("InterestPaymentFrequency", "DayCountFraction"),
    "IssueDate": ("SettlementDate", "BusinessDayConvention"),
    "SettlementDate": ("IssueDate", "BusinessDayConvention"),
    "InterestPaymentDate": ("IssueDate", "InterestPaymentFrequency", "BusinessDayConvention"),
    "IssueAmount": ("Currency", "NominalAmountPerBond"),
    "NominalAmountPerBond": ("Currency",),
    "MinimumSubscription": ("NominalAmountPerBond", "Currency"),
    "IssuePrice": ("NominalAmountPerBond",),
    "BusinessDayConvention": ("BusinessDayLocation",),
    "BusinessDayLocation": ("BusinessDayConvention",),
    "AmortizationType": ("Maturity",),
    "Issuer": ("Parent",),
    "Parent": ("Issuer",),
    "InterestPaymentFrequency": ("Coupon",),
    "DayCountFraction": ("Coupon",),
}


@dataclass
class AdjudicationOutcome:
    response: AdjudicationResponse | None
    error_type: str | None = None
    error: str | None = None


class MismatchAnalyzer:
    def __init__(self, llm: LLMClient, settings: Settings, cache: JsonCache | None = None) -> None:
        self._llm = llm
        self._settings = settings
        self._cache = cache
        self._memo: dict[str, AdjudicationOutcome] = {}
        self.stats = {"suspected_mismatches": 0, "llm_calls": 0, "reused": 0, "from_cache": 0,
                      "failed": 0, "invalid_responses": 0}

    def adjudicate(self, results: list[ReconciliationResult], extraction: DocumentExtraction) -> None:
        for row in results:
            if row.initial_result != InitialResult.MISMATCH or row.final_status is not None:
                continue
            self.stats["suspected_mismatches"] += 1
            payload = build_payload(row, extraction)
            signature = JsonCache.make_key(CACHE_NAMESPACE, self._llm.model, ADJUDICATION_PROMPT_VERSION, payload)
            if signature in self._memo:
                self.stats["reused"] += 1
                self._apply(row, self._memo[signature], "llm_reused")
                continue
            outcome, source = self._resolve(signature, payload, row)
            if outcome.response is not None:
                self._memo[signature] = outcome
            self._apply(row, outcome, source)

    # ------------------------------------------------------------------ LLM

    def _resolve(self, signature: str, payload: dict, row: ReconciliationResult) -> tuple[AdjudicationOutcome, str]:
        cached = self._cache.get(CACHE_NAMESPACE, signature) if self._cache else None
        if cached and isinstance(cached.get("content"), str):
            try:
                response = AdjudicationResponse.model_validate(parse_json_object(cached["content"]))
                self.stats["from_cache"] += 1
                return AdjudicationOutcome(response), "cache"
            except (LLMResponseFormatError, ValidationError):
                pass

        user = build_adjudication_user_prompt(payload)
        label = f"{row.document} trade {row.trade_id} {row.field}"
        last_problem = ""
        for attempt in (1, 2):  # second attempt is a corrective retry for an invalid response
            prompt = user if attempt == 1 else (
                f"{user}\n\nYour previous reply could not be used ({last_problem}). Return only the JSON verdict "
                "with the exact keys and allowed values."
            )
            try:
                self.stats["llm_calls"] += 1
                response = self._llm.complete_json(
                    system=ADJUDICATION_SYSTEM_PROMPT, user=prompt, purpose="adjudication",
                    max_tokens=self._settings.adjudication_max_tokens,
                )
            except LLMError as exc:
                if exc.fatal:
                    raise
                self.stats["failed"] += 1
                logger.error("Adjudication call failed for %s: %s", label, exc)
                return AdjudicationOutcome(None, f"LLM_{exc.category}", str(exc)), "llm_failed"
            try:
                verdict = AdjudicationResponse.model_validate(parse_json_object(response.content))
            except (LLMResponseFormatError, ValidationError) as exc:
                self.stats["invalid_responses"] += 1
                last_problem = str(exc).splitlines()[0][:300]
                logger.warning("Invalid adjudication response for %s (attempt %d): %s", label, attempt, last_problem)
                continue
            if self._cache:
                self._cache.set(CACHE_NAMESPACE, signature, {"field": row.field, "model": self._llm.model,
                                                             "content": response.content})
            logger.info("Adjudicated %s: %s (%s, confidence %s)", label, verdict.verdict, verdict.category,
                        verdict.confidence,
                        extra={"event": "adjudication", "document": row.document, "trade_id": row.trade_id,
                               "field": row.field, "verdict": verdict.verdict})
            return AdjudicationOutcome(verdict), "llm"
        self.stats["failed"] += 1
        return AdjudicationOutcome(None, "LLM_INVALID_RESPONSE", f"Invalid adjudication response: {last_problem}"), \
            "llm_failed"

    # ------------------------------------------------------------------ decision

    def _apply(self, row: ReconciliationResult, outcome: AdjudicationOutcome, source: str) -> None:
        row.adjudication_source = source
        if outcome.response is None:
            row.final_status = FinalStatus.UNRESOLVED
            row.error_type, row.error_details = outcome.error_type, outcome.error
            return
        verdict = outcome.response
        row.llm_verdict = verdict.verdict
        row.llm_category = verdict.category
        row.llm_explanation = verdict.explanation
        row.llm_evidence = list(verdict.supporting_evidence)
        row.llm_confidence = verdict.confidence
        status, guard = decide(row, verdict)
        row.final_status = status
        if guard:
            row.error_type, row.error_details = "GUARDRAIL", guard


def decide(row: ReconciliationResult, verdict: AdjudicationResponse) -> tuple[FinalStatus, str | None]:
    if verdict.verdict == "INSUFFICIENT_EVIDENCE":
        return FinalStatus.UNRESOLVED, "LLM reported insufficient evidence to decide"
    if not verdict.supporting_evidence:
        return FinalStatus.UNRESOLVED, "LLM verdict did not cite supporting evidence"
    if verdict.verdict == "TRUE_MISMATCH":
        return FinalStatus.TRUE_MISMATCH, None
    if verdict.confidence == "low":
        return FinalStatus.UNRESOLVED, "Low-confidence False Alert held for human review"
    if row.hard_difference and not _verified_business_day_roll(row, verdict):
        return FinalStatus.UNRESOLVED, (
            "LLM proposed a False Alert, but the values still differ after normalisation in the same unit; "
            "held for human review"
        )
    return FinalStatus.FALSE_ALERT, None


def _verified_business_day_roll(row: ReconciliationResult, verdict: AdjudicationResponse) -> bool:
    """Accept a date False Alert only if the term-sheet date is a weekend rolled by at most 3 days."""
    spec = SPEC_BY_NAME.get(row.field)
    if verdict.category != "BUSINESS_DAY_ADJUSTMENT" or spec is None or spec.kind not in (FieldKind.DATE, FieldKind.MATURITY):
        return False
    try:
        ts = date.fromisoformat(row.term_sheet_value or "")
        bk = date.fromisoformat(row.booking_normalized or "")
    except ValueError:
        return False
    return ts.weekday() >= 5 and 0 < abs((bk - ts).days) <= 3


def build_payload(row: ReconciliationResult, extraction: DocumentExtraction) -> dict:
    """Facts sent to the adjudication call. Trade identifiers are excluded so identical breaks share a verdict."""
    spec = SPEC_BY_NAME[row.field]
    related = {}
    for name in RELATED_TERMS.get(row.field, ()):
        extracted = extraction.fields.get(name)
        if extracted is None:
            continue
        if extracted.status == ExtractionStatus.EXTRACTED:
            related[name] = {
                "value": display_value(extracted.value),
                "evidence": extracted.evidence[0].quote if extracted.evidence else None,
            }
        elif extracted.status == ExtractionStatus.NOT_IN_DOCUMENT:
            related[name] = {"value": None, "note": "not stated in the term sheet"}
    return {
        "field": row.field,
        "definition": spec.description,
        "scope": row.field_scope,
        "term_sheet": {
            "value": row.term_sheet_value,
            "raw_value": row.term_sheet_raw_value,
            "basis": row.value_basis,
            "evidence_quotes": row.evidence_quotes,
            "evidence_pages": row.evidence_pages,
            "extraction_note": row.extraction_note,
        },
        "booking": {"raw_value": row.booking_value, "normalized_value": row.booking_normalized},
        "deterministic_check": {
            "method": row.comparison_method,
            "finding": row.initial_reason,
            "values_differ_in_same_unit": row.hard_difference,
            "possible_equivalence": row.candidate_equivalence,
        },
        "related_terms": related,
    }
