"""First LLM pass: structured field extraction with evidence.

Pipeline for one document:

1. select the relevant excerpts (``excerpt_selector``) within the token budget;
2. call the LLM once (cached by document hash + prompt + model);
3. recover from malformed JSON with one corrective retry;
4. validate every field independently against its strict Pydantic type –
   a failure marks only that field as a *Parsing Error*;
5. verify that every evidence quote exists in the document and locate its page;
6. check that "stated" numeric/date/ISIN values literally appear in their quotes.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .cache import JsonCache
from .config import Settings
from .document_parser import ParsedDocument
from .evidence import EvidenceLocator
from .excerpt_selector import select_excerpts
from .field_catalog import FIELD_NAMES, FIELD_SPECS, PERPETUAL, FieldKind, FieldSpec
from .llm_client import LLMClient, LLMError, LLMResponseFormatError, parse_json_object
from .normalizer import (
    canonical_chars,
    decimals_equal,
    find_amounts_in_text,
    find_dates_in_text,
    find_percentages_in_text,
    normalize_for_match,
    normalize_isin,
    strip_all_whitespace,
)
from .prompts import EXTRACTION_PROMPT_VERSION, EXTRACTION_SYSTEM_PROMPT, build_extraction_user_prompt
from .schemas import (
    SCHEMA_VERSION,
    VALUE_TYPES,
    DocumentExtraction,
    ExtractedField,
    ExtractionStatus,
    FieldExtraction,
    VerifiedEvidence,
)

logger = logging.getLogger(__name__)

CACHE_NAMESPACE = "extractions"


@lru_cache(maxsize=None)
def _adapter(kind: FieldKind) -> TypeAdapter:
    return TypeAdapter(ExtractedField[VALUE_TYPES[kind]])  # type: ignore[index]


def format_validation_errors(exc: ValidationError) -> list[str]:
    messages = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "field"
        message = error["msg"].removeprefix("Value error, ")
        messages.append(f"{location}: {message}")
    return messages


class ExtractionService:
    def __init__(self, llm: LLMClient, settings: Settings, cache: JsonCache | None = None) -> None:
        self._llm = llm
        self._settings = settings
        self._cache = cache
        self.invalid_responses = 0  # responses received but unusable (malformed JSON / wrong shape)

    def extract(self, document: ParsedDocument) -> DocumentExtraction:
        selection = select_excerpts(document, self._settings.extraction_max_chars)
        missing = [name for name, found in selection.field_coverage.items() if not found]
        logger.info(
            "Extraction input for %s: %s, %d chars from pages %s; no candidate passage for %s",
            document.name, selection.strategy, selection.chars, selection.pages, missing or "none",
            extra={"event": "excerpt_selection", "document": document.name, "strategy": selection.strategy,
                   "chars": selection.chars, "pages": selection.pages},
        )
        user_prompt = build_extraction_user_prompt(document.name, document.page_count, selection)
        base = DocumentExtraction(
            document=document.name,
            document_path=str(document.path),
            sha256=document.sha256,
            file_type=document.file_type,
            page_count=document.page_count,
            model=self._llm.model,
            prompt_version=EXTRACTION_PROMPT_VERSION,
            extracted_at=datetime.now(timezone.utc).isoformat(),
            status="OK",
            strategy=selection.strategy,
            pages_sent=selection.pages,
            chars_sent=selection.chars,
            field_coverage=selection.field_coverage,
        )

        cache_key = JsonCache.make_key(
            CACHE_NAMESPACE, self._llm.model, EXTRACTION_PROMPT_VERSION, SCHEMA_VERSION,
            EXTRACTION_SYSTEM_PROMPT, user_prompt,
        )
        cached = self._cache.get(CACHE_NAMESPACE, cache_key) if self._cache else None
        envelope: dict | None = None
        if cached and isinstance(cached.get("content"), str):
            try:
                envelope = self._parse_envelope(cached["content"])
                base.from_cache = True
                logger.info("Using cached extraction for %s (document hash %s…)", document.name, document.sha256[:12])
            except LLMResponseFormatError:
                envelope = None

        if envelope is None:
            try:
                envelope, content = self._call_with_repair(user_prompt, base)
            except LLMError as exc:
                if exc.fatal:
                    raise
                base.status = "FAILED"
                base.error = f"{exc.category}: {exc}"
                logger.error("Extraction failed for %s: %s", document.name, base.error)
                return base
        else:
            content = None

        base.fields = self._validate_fields(envelope, document)
        if content is not None:
            envelope = self._repair_fields(envelope, base, document, user_prompt)
            content = json.dumps({"fields": envelope}, ensure_ascii=False)
        parsing_errors = [f.field for f in base.fields.values() if f.status == ExtractionStatus.PARSING_ERROR]
        if content is not None and self._cache and not parsing_errors:
            self._cache.set(CACHE_NAMESPACE, cache_key, {"document": document.name, "model": self._llm.model,
                                                         "content": content})
        logger.info(
            "Extraction for %s: %d extracted, %d not in document, %d parsing error(s)%s",
            document.name,
            sum(f.status == ExtractionStatus.EXTRACTED for f in base.fields.values()),
            sum(f.status == ExtractionStatus.NOT_IN_DOCUMENT for f in base.fields.values()),
            len(parsing_errors),
            f" ({', '.join(parsing_errors)})" if parsing_errors else "",
            extra={"event": "extraction_validated", "document": document.name, "parsing_errors": parsing_errors},
        )
        return base

    # ------------------------------------------------------------------ LLM call

    def _call_with_repair(self, user_prompt: str, base: DocumentExtraction) -> tuple[dict, str]:
        response = self._llm.complete_json(
            system=EXTRACTION_SYSTEM_PROMPT, user=user_prompt, purpose="extraction",
            max_tokens=self._settings.extraction_max_tokens,
        )
        base.llm_calls += 1
        try:
            return self._parse_envelope(response.content), response.content
        except LLMResponseFormatError as first_error:
            self.invalid_responses += 1
            logger.warning("Extraction response for %s was malformed (%s); asking the model to repair it",
                           base.document, first_error)
            repair_prompt = (
                f"{user_prompt}\n\nYour previous reply could not be used: {first_error}. "
                'Reply again with only the JSON object {"fields": {...}} containing every field.'
            )
            response = self._llm.complete_json(
                system=EXTRACTION_SYSTEM_PROMPT, user=repair_prompt, purpose="extraction_repair",
                max_tokens=self._settings.extraction_max_tokens,
            )
            base.llm_calls += 1
            try:
                return self._parse_envelope(response.content), response.content
            except LLMResponseFormatError:
                self.invalid_responses += 1
                raise

    def _repair_fields(self, envelope: dict, base: DocumentExtraction, document: ParsedDocument,
                       user_prompt: str) -> dict:
        """One targeted retry for fields that failed validation; a fix is kept only if it passes every check."""
        failed = {name: f for name, f in base.fields.items() if f.status == ExtractionStatus.PARSING_ERROR}
        if not failed:
            return envelope
        problems = "\n".join(f"- {name}: {'; '.join(f.errors)}" for name, f in failed.items())
        repair_prompt = (
            f"{user_prompt}\n\nYour previous answer had problems in these fields:\n{problems}\n"
            'Return a JSON object {"fields": {...}} containing ONLY these fields, corrected. Every quote must be '
            "copied exactly from the document text above (no ellipses, no cleaning up of garbled text); if no exact "
            "quote supports a value, return null for that field."
        )
        logger.info("Asking the model to repair %d field(s) for %s: %s", len(failed), base.document, sorted(failed))
        base.repair_attempted = True
        try:
            response = self._llm.complete_json(
                system=EXTRACTION_SYSTEM_PROMPT, user=repair_prompt, purpose="extraction_field_repair",
                max_tokens=self._settings.extraction_max_tokens,
            )
            base.llm_calls += 1
            repaired = self._parse_envelope(response.content)
        except LLMResponseFormatError as exc:
            self.invalid_responses += 1
            logger.warning("Field repair response for %s was unusable: %s", base.document, exc)
            return envelope
        except LLMError as exc:
            if exc.fatal:
                raise
            logger.warning("Field repair call for %s failed: %s", base.document, exc)
            return envelope

        merged = dict(envelope)
        candidate_raw = dict(envelope)
        candidate_raw.update({k: v for k, v in repaired.items() if k in failed})
        candidates = self._validate_fields(candidate_raw, document)
        for name in failed:
            if name in repaired and candidates[name].status != ExtractionStatus.PARSING_ERROR:
                fixed = candidates[name]
                fixed.warnings.append(f"corrected in repair pass (first attempt: {'; '.join(failed[name].errors)})")
                base.fields[name] = fixed
                base.repaired_fields.append(name)
                merged[name] = repaired[name]
            else:
                base.fields[name].warnings.append("repair pass did not produce a valid value")
        logger.info("Repair pass for %s fixed %s", base.document, base.repaired_fields or "nothing")
        return merged

    @staticmethod
    def _parse_envelope(content: str) -> dict:
        payload = parse_json_object(content)
        fields = payload.get("fields")
        if fields is None and sum(name in payload for name in FIELD_NAMES) >= len(FIELD_NAMES) // 2:
            fields = payload  # model omitted the wrapper but returned the fields themselves
        if not isinstance(fields, dict):
            raise LLMResponseFormatError('JSON object has no "fields" object')
        return fields

    # ------------------------------------------------------------------ validation

    def _validate_fields(self, fields: dict, document: ParsedDocument) -> dict[str, FieldExtraction]:
        locator = EvidenceLocator(document)
        results = {spec.name: self._validate_field(spec, fields.get(spec.name), locator) for spec in FIELD_SPECS}
        _check_trade_level_semantics(results)
        unexpected = sorted(set(fields) - set(FIELD_NAMES))
        if unexpected:
            logger.warning("Ignoring unexpected fields in LLM output: %s", unexpected)
        return results

    def _validate_field(self, spec: FieldSpec, raw: Any, locator: EvidenceLocator) -> FieldExtraction:
        result = FieldExtraction(field=spec.name, status=ExtractionStatus.PARSING_ERROR, llm_output=raw)
        if raw is None:
            result.error_type = "MISSING_FIELD"
            result.errors = ["field missing from the LLM response"]
            return result
        if not isinstance(raw, dict):
            result.error_type = "SCHEMA_VALIDATION"
            result.errors = [f"expected an object, got {type(raw).__name__}"]
            return result

        try:
            parsed = _adapter(spec.kind).validate_python(raw)
        except ValidationError as exc:
            result.error_type = "SCHEMA_VALIDATION"
            result.errors = format_validation_errors(exc)
            return result

        result.value, result.raw_value = parsed.value, parsed.raw_value
        result.basis, result.note = parsed.basis, parsed.note
        ignored = sorted(set(raw) - {"value", "raw_value", "evidence", "basis", "note"})
        if ignored:
            result.warnings.append(f"ignored unexpected keys: {ignored}")

        for quote in parsed.evidence:
            match = locator.locate(quote.quote, quote.page)
            result.evidence.append(
                VerifiedEvidence(
                    quote=quote.quote, claimed_page=quote.page, page=match.page, verified=match.found,
                    match_type=match.match_type, text_view=match.text_view,
                )
            )
            if match.found and quote.page is not None and match.page != quote.page:
                result.warnings.append(f"LLM cited page {quote.page}; quote found on page {match.page}")

        if parsed.value is None:
            result.status = ExtractionStatus.NOT_IN_DOCUMENT
            return result

        unverified = [e.quote for e in result.evidence if not e.verified]
        if unverified:
            result.status = ExtractionStatus.PARSING_ERROR
            result.error_type = "EVIDENCE_NOT_FOUND"
            result.errors = [f"evidence quote not found in the document: {q[:200]!r}" for q in unverified]
            return result

        result.grounding = self._grounding(spec, parsed.value, parsed.basis, [e.quote for e in result.evidence])
        if result.grounding == "NOT_GROUNDED":
            result.status = ExtractionStatus.PARSING_ERROR
            result.error_type = "VALUE_NOT_IN_EVIDENCE"
            result.errors = [f"stated value {parsed.value!s} does not appear in its quoted evidence"]
            return result

        if parsed.raw_value and not _contained(parsed.raw_value, [e.quote for e in result.evidence]):
            result.warnings.append("raw_value is not a substring of the quoted evidence")
        result.status = ExtractionStatus.EXTRACTED
        return result

    @staticmethod
    def _grounding(spec: FieldSpec, value: Any, basis: str | None, quotes: list[str]) -> str:
        """Check that an explicitly stated value is literally present in its own quote."""
        if basis != "stated":
            return "NOT_APPLICABLE"
        text = " ".join(quotes)
        if spec.kind == FieldKind.ISIN:
            return "GROUNDED" if value in normalize_isin(text) else "NOT_GROUNDED"
        if spec.kind == FieldKind.MATURITY and value == PERPETUAL:
            lowered = canonical_chars(text).lower()
            return "GROUNDED" if any(w in lowered for w in ("perpetual", "undated", "no fixed maturity")) else "NOT_GROUNDED"
        if spec.kind in (FieldKind.DATE, FieldKind.MATURITY) and isinstance(value, date):
            return "GROUNDED" if value in find_dates_in_text(text) else "NOT_GROUNDED"
        if spec.kind in (FieldKind.PERCENT, FieldKind.PRICE) and isinstance(value, Decimal):
            candidates = find_percentages_in_text(text) + find_amounts_in_text(text)
            return "GROUNDED" if any(decimals_equal(value, c) for c in candidates) else "NOT_GROUNDED"
        if spec.kind == FieldKind.AMOUNT and isinstance(value, Decimal):
            return "GROUNDED" if any(decimals_equal(value, c) for c in find_amounts_in_text(text)) else "NOT_GROUNDED"
        return "NOT_APPLICABLE"


ISSUE_LEVEL_AMOUNTS = ("IssueAmount", "NominalAmountPerBond", "MinimumSubscription")


def _check_trade_level_semantics(results: dict[str, FieldExtraction]) -> None:
    """Reject a trade notional that is really an issue-level amount (same clause or same value).

    Issue size, denomination and minimum subscription describe the whole bond, never a single trade. The value
    is not silently dropped: the field becomes a Parsing Error so the report shows the extraction defect.
    """
    notional = results.get("Notional")
    if notional is None or notional.status != ExtractionStatus.EXTRACTED:
        return
    notional_quotes = [normalize_for_match(e.quote) for e in notional.evidence]
    for name in ISSUE_LEVEL_AMOUNTS:
        other = results.get(name)
        if other is None or other.status != ExtractionStatus.EXTRACTED:
            continue
        other_quotes = [normalize_for_match(e.quote) for e in other.evidence]
        shared_clause = any(a in b or b in a for a in notional_quotes for b in other_quotes)
        same_value = isinstance(other.value, Decimal) and decimals_equal(notional.value, other.value)
        if shared_clause or same_value:
            notional.status = ExtractionStatus.PARSING_ERROR
            notional.error_type = "FIELD_SEMANTICS"
            notional.errors = [
                f"Notional {notional.value} was taken from the {name} {'clause' if shared_clause else 'value'}; "
                "issue-level amounts are not a trade notional"
            ]
            logger.warning("Rejected extracted Notional: %s", notional.errors[0])
            return


def _contained(needle: str, quotes: list[str]) -> bool:
    joined = " ".join(quotes)
    return (
        normalize_for_match(needle) in normalize_for_match(joined)
        or strip_all_whitespace(needle) in strip_all_whitespace(joined)
    )
