"""Deterministic, field-level reconciliation of term-sheet terms against bookings.

Each field kind has a comparator registered in ``ReconciliationEngine``. A
comparator returns an initial result:

* ``MATCH`` – equal after well-defined normalisation (dates, numeric precision,
  case/whitespace, synonyms of standard conventions, list order);
* ``MISMATCH`` – a suspected break, sent to the second LLM pass. The comparison
  records whether the values *differ after normalisation in the same unit*
  (``hard_difference``) or whether a plausible representation difference was
  detected (``candidate_equivalence``, e.g. a decimal rate versus a percentage);
* ``NOT_COMPARABLE`` – the term sheet does not state the field; reported as
  *Unverifiable*, never as a match or mismatch;
* ``PARSING_ERROR`` – either side failed validation.

New field kinds or field-specific rules are added with ``register_kind`` /
``register_field`` without touching the engine.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from .field_catalog import FIELD_SPECS, PERPETUAL, FieldKind, FieldSpec
from .normalizer import (
    canonical_amortization,
    canonical_business_day_convention,
    canonical_day_count,
    canonical_frequency,
    decimals_equal,
    display_value,
    format_decimal,
    location_key,
    normalize_entity_name,
)
from .schemas import (
    BookingTrade,
    BookingValue,
    DocumentExtraction,
    ExtractionStatus,
    FieldExtraction,
    FinalStatus,
    InitialResult,
    ReconciliationResult,
)


@dataclass(frozen=True)
class Comparison:
    method: str
    result: InitialResult
    reason: str
    term_sheet_normalized: str | None = None
    booking_normalized: str | None = None
    hard_difference: bool = False
    candidate_equivalence: str | None = None


@dataclass(frozen=True)
class ComparisonContext:
    extraction: DocumentExtraction
    trade: BookingTrade

    def term(self, name: str) -> Any:
        extracted = self.extraction.fields.get(name)
        if extracted is None or extracted.status != ExtractionStatus.EXTRACTED:
            return None
        return extracted.value


Comparator = Callable[[FieldSpec, Any, Any, ComparisonContext], Comparison]

_SCALE_FACTORS = {
    Decimal(10) ** 2: "x100 (percent vs decimal / basis points)",
    Decimal(10) ** 3: "x1,000 (thousands)",
    Decimal(10) ** 5: "x100,000 (lakh)",
    Decimal(10) ** 6: "x1,000,000 (millions)",
    Decimal(10) ** 7: "x10,000,000 (crore)",
    Decimal(10) ** 9: "x1,000,000,000 (billions)",
}


def _match(method: str, reason: str, ts: str | None, bk: str | None) -> Comparison:
    return Comparison(method, InitialResult.MATCH, reason, ts, bk)


def _mismatch(method: str, reason: str, ts: str | None, bk: str | None, *, hard: bool,
              candidate: str | None = None) -> Comparison:
    return Comparison(method, InitialResult.MISMATCH, reason, ts, bk, hard_difference=hard,
                      candidate_equivalence=candidate)


def _scale_relation(a: Decimal, b: Decimal) -> str | None:
    if a == 0 or b == 0:
        return None
    for factor, label in _SCALE_FACTORS.items():
        if decimals_equal(a * factor, b) or decimals_equal(b * factor, a):
            return label
    return None


# --------------------------------------------------------------------------- comparators


def compare_text(spec: FieldSpec, ts: str, bk: str, ctx: ComparisonContext) -> Comparison:
    method = "entity_name_normalized"
    if normalize_entity_name(ts) == normalize_entity_name(bk):
        return _match(method, "Equal ignoring case, punctuation and legal-suffix abbreviations", ts, bk)
    return _mismatch(method, "Names differ after normalisation", ts, bk, hard=False)


def compare_isin(spec: FieldSpec, ts: str, bk: str, ctx: ComparisonContext) -> Comparison:
    if ts == bk:
        return _match("isin_exact", "Identical ISIN after removing spaces", ts, bk)
    return _mismatch("isin_exact", "Different ISINs", ts, bk, hard=True)


def compare_date(spec: FieldSpec, ts: date, bk: date, ctx: ComparisonContext) -> Comparison:
    if ts == bk:
        return _match("date_equal", "Same calendar date after parsing both formats", ts.isoformat(), bk.isoformat())
    days = (bk - ts).days
    return _mismatch("date_equal", f"Dates differ by {days:+d} day(s)", ts.isoformat(), bk.isoformat(), hard=True)


def compare_maturity(spec: FieldSpec, ts: Any, bk: Any, ctx: ComparisonContext) -> Comparison:
    method = "maturity_perpetual_aware"
    if ts == PERPETUAL and bk == PERPETUAL:
        return _match(method, "Both perpetual", PERPETUAL, PERPETUAL)
    if ts == PERPETUAL:
        return _mismatch(
            method,
            "Term sheet describes a perpetual instrument (no fixed maturity) but the booking carries a fixed "
            "maturity date",
            PERPETUAL, display_value(bk), hard=True,
        )
    if bk == PERPETUAL:
        return _mismatch(method, "Booking marks the bond perpetual but the term sheet states a maturity date",
                         display_value(ts), PERPETUAL, hard=True)
    return compare_date(spec, ts, bk, ctx)


def compare_percent(spec: FieldSpec, ts: Decimal, bk: Decimal, ctx: ComparisonContext) -> Comparison:
    method = "percent_numeric"
    a, b = format_decimal(ts), format_decimal(bk)
    if decimals_equal(ts, bk):
        return _match(method, "Equal in percent units", f"{a}%", b)
    if decimals_equal(bk * 100, ts):
        return _mismatch(method, f"Booking {b} equals {a}% expressed as a decimal rate", f"{a}%", b, hard=False,
                         candidate="decimal_rate_vs_percent")
    if decimals_equal(ts * 100, bk):
        return _mismatch(method, f"Booking {b} equals {a}% expressed in basis points", f"{a}%", b, hard=False,
                         candidate="basis_points_vs_percent")
    diff = format_decimal(bk - ts)
    return _mismatch(method, f"Rates differ by {diff} percentage points", f"{a}%", b, hard=True)


def compare_price(spec: FieldSpec, ts: Decimal, bk: Decimal, ctx: ComparisonContext) -> Comparison:
    method = "price_percent_of_par"
    a, b = format_decimal(ts), format_decimal(bk)
    if decimals_equal(ts, bk):
        return _match(method, "Equal as a percentage of par", f"{a}%", b)
    if decimals_equal(bk * 100, ts):
        return _mismatch(method, f"Booking {b} equals {a}% of par expressed as a fraction", f"{a}%", b,
                         hard=False, candidate="fraction_vs_percent_of_par")
    denomination = ctx.term("NominalAmountPerBond")
    if isinstance(denomination, Decimal) and decimals_equal(ts * denomination / 100, bk):
        return _mismatch(method, f"Booking {b} equals {a}% of the {format_decimal(denomination)} denomination",
                         f"{a}%", b, hard=False, candidate="absolute_price_vs_percent_of_par")
    return _mismatch(method, f"Prices differ by {format_decimal(bk - ts)} points of par", f"{a}%", b, hard=True)


def compare_amount(spec: FieldSpec, ts: Decimal, bk: Decimal, ctx: ComparisonContext) -> Comparison:
    method = "amount_exact"
    a, b = format_decimal(ts), format_decimal(bk)
    if decimals_equal(ts, bk):
        return _match(method, "Equal amounts", a, b)
    scale = _scale_relation(ts, bk)
    if scale:
        return _mismatch(method, f"Amounts differ by a unit/scale factor {scale}", a, b, hard=False,
                         candidate=f"unit_scale {scale}")
    return _mismatch(method, f"Amounts differ by {format_decimal(bk - ts)}", a, b, hard=True)


def compare_currency(spec: FieldSpec, ts: str, bk: str, ctx: ComparisonContext) -> Comparison:
    if ts == bk:
        return _match("currency_iso", "Same ISO 4217 currency", ts, bk)
    return _mismatch("currency_iso", "Different currencies", ts, bk, hard=True)


def _vocabulary_comparator(method: str, canonicalise: Callable[[str], str | None]) -> Comparator:
    def compare(spec: FieldSpec, ts: str, bk: str, ctx: ComparisonContext) -> Comparison:
        ts_canon, bk_canon = canonicalise(ts) or ts, canonicalise(bk)
        if bk_canon is None:
            return _mismatch(method, f"Booking value {bk!r} is not a recognised standard term", ts_canon, bk,
                             hard=False)
        if ts_canon == bk_canon:
            reason = "Identical" if ts == bk else f"Equivalent terms ({ts!r} = {bk!r})"
            return _match(method, reason, ts_canon, bk_canon)
        same_family = ts_canon.split(" (")[0] == bk_canon.split(" (")[0]
        return _mismatch(method, f"{ts_canon!r} vs {bk_canon!r}", ts_canon, bk_canon, hard=not same_family,
                         candidate="same_convention_family" if same_family else None)

    return compare


def compare_business_day_convention(spec: FieldSpec, ts: str, bk: str, ctx: ComparisonContext) -> Comparison:
    method = "business_day_convention"
    ts_canon, bk_canon = canonical_business_day_convention(ts), canonical_business_day_convention(bk)
    if ts_canon and bk_canon:
        if ts_canon == bk_canon:
            return _match(method, f"Same convention ({ts_canon})", ts_canon, bk_canon)
        return _mismatch(method, f"{ts_canon} vs {bk_canon}", ts_canon, bk_canon, hard=True)
    return _mismatch(method, "Convention described in non-standard or mixed terms; needs semantic review",
                     ts, bk, hard=False)


def compare_locations(spec: FieldSpec, ts: list[str], bk: list[str], ctx: ComparisonContext) -> Comparison:
    method = "location_set"
    a, b = " | ".join(ts), " | ".join(bk)
    if location_key(ts) == location_key(bk):
        return _match(method, "Same set of business centres (order and case ignored)", a, b)
    return _mismatch(method, "Different sets of business centres", a, b, hard=False)


def compare_notional(spec: FieldSpec, ts: Any, bk: Decimal, ctx: ComparisonContext) -> Comparison:
    """Trade notional: never compared with the issue size; checked for consistency with the denomination."""
    method = "notional_trade_level"
    if ts is not None:
        return compare_amount(spec, ts, bk, ctx)
    b = format_decimal(bk)
    denomination = ctx.term("NominalAmountPerBond")
    if not isinstance(denomination, Decimal) or denomination <= 0:
        return Comparison(method, InitialResult.NOT_COMPARABLE,
                          "Term sheet states no trade notional and no denomination to check it against", None, b)
    bonds = bk / denomination
    d = format_decimal(denomination)
    if bonds != bonds.to_integral_value():
        return _mismatch(
            method,
            f"Booked notional {b} is not a whole number of bonds: {format_decimal(bonds)} x the {d} denomination "
            "stated in the term sheet",
            f"denomination {d}", b, hard=True,
        )
    reason = (f"Term sheet does not state a trade notional (the issue size is a different quantity). "
              f"Consistency check passed: {b} = {format_decimal(bonds)} bond(s) x {d}.")
    minimum = ctx.term("MinimumSubscription")
    if isinstance(minimum, Decimal) and bk < minimum:
        reason += f" Note: below the primary-market minimum subscription of {format_decimal(minimum)}."
    return Comparison(method, InitialResult.NOT_COMPARABLE, reason, f"denomination {d}", b)


DEFAULT_COMPARATORS: dict[FieldKind, Comparator] = {
    FieldKind.TEXT: compare_text,
    FieldKind.ISIN: compare_isin,
    FieldKind.DATE: compare_date,
    FieldKind.MATURITY: compare_maturity,
    FieldKind.PERCENT: compare_percent,
    FieldKind.PRICE: compare_price,
    FieldKind.AMOUNT: compare_amount,
    FieldKind.CURRENCY: compare_currency,
    FieldKind.FREQUENCY: _vocabulary_comparator("frequency_vocabulary", canonical_frequency),
    FieldKind.DAY_COUNT: _vocabulary_comparator("day_count_vocabulary", canonical_day_count),
    FieldKind.AMORTIZATION: _vocabulary_comparator("amortization_vocabulary", canonical_amortization),
    FieldKind.BUSINESS_DAY_CONVENTION: compare_business_day_convention,
    FieldKind.LOCATIONS: compare_locations,
}
DEFAULT_FIELD_OVERRIDES: dict[str, Comparator] = {"Notional": compare_notional}


# --------------------------------------------------------------------------- engine


class ReconciliationEngine:
    def __init__(
        self,
        comparators: dict[FieldKind, Comparator] | None = None,
        field_overrides: dict[str, Comparator] | None = None,
        specs: tuple[FieldSpec, ...] = FIELD_SPECS,
    ) -> None:
        self._comparators = dict(DEFAULT_COMPARATORS if comparators is None else comparators)
        self._overrides = dict(DEFAULT_FIELD_OVERRIDES if field_overrides is None else field_overrides)
        self._specs = specs

    def register_kind(self, kind: FieldKind, comparator: Comparator) -> None:
        self._comparators[kind] = comparator

    def register_field(self, name: str, comparator: Comparator) -> None:
        self._overrides[name] = comparator

    def reconcile(
        self, extraction: DocumentExtraction, trades: list[BookingTrade], booking_source: str
    ) -> list[ReconciliationResult]:
        return [
            self._reconcile_field(spec, extraction, trade, booking_source)
            for trade in trades
            for spec in self._specs
        ]

    def _reconcile_field(
        self, spec: FieldSpec, extraction: DocumentExtraction, trade: BookingTrade, booking_source: str
    ) -> ReconciliationResult:
        booking: BookingValue = trade.values.get(spec.name, BookingValue())
        row = ReconciliationResult(
            document=extraction.document,
            trade_id=trade.trade_id,
            isin=trade.isin,
            field=spec.name,
            field_scope=spec.scope.value,
            booking_value=_raw_display(booking.raw),
            booking_normalized=display_value(booking.value),
            initial_result=InitialResult.NOT_COMPARABLE,
            booking_source=booking_source,
        )

        if extraction.status != "OK":
            return _parsing_error(row, "EXTRACTION_FAILED", f"Term sheet extraction failed: {extraction.error}")

        extracted = extraction.fields.get(spec.name)
        if extracted is not None:
            _attach_extraction(row, extracted)
        if extracted is None or extracted.status == ExtractionStatus.PARSING_ERROR:
            errors = "; ".join(extracted.errors) if extracted else "field missing from extraction"
            error_type = extracted.error_type if extracted else "MISSING_FIELD"
            return _parsing_error(row, error_type or "SCHEMA_VALIDATION", f"Term sheet extraction: {errors}")
        if booking.error:
            return _parsing_error(row, "BOOKING_VALUE_INVALID", f"Booking value {booking.raw!r}: {booking.error}")

        ctx = ComparisonContext(extraction, trade)
        override = self._overrides.get(spec.name)
        ts_value = extracted.value if extracted.status == ExtractionStatus.EXTRACTED else None

        if booking.value is None:
            if ts_value is None:
                return _apply(row, Comparison("presence", InitialResult.NOT_COMPARABLE,
                                              "Neither the term sheet nor the booking provides a value"))
            return _apply(row, _mismatch("presence", "Booking has no value for a term stated in the term sheet",
                                         display_value(ts_value), None, hard=False, candidate=None))

        if override is not None:
            if spec.name == "Notional" and ts_value is None:
                # The consistency check relies on the denomination, so show that evidence on the row.
                denomination = extraction.fields.get("NominalAmountPerBond")
                if denomination is not None and denomination.status == ExtractionStatus.EXTRACTED:
                    _attach_extraction(row, denomination, label="NominalAmountPerBond")
            return _apply(row, override(spec, ts_value, booking.value, ctx))

        if ts_value is None:
            reason = "Term sheet does not state this field"
            if extracted.note:
                reason += f" ({extracted.note})"
            return _apply(row, Comparison("presence", InitialResult.NOT_COMPARABLE, reason, None,
                                          display_value(booking.value)))

        comparator = self._comparators.get(spec.kind)
        if comparator is None:
            return _apply(row, Comparison("none", InitialResult.NOT_COMPARABLE,
                                          f"No comparison rule registered for kind {spec.kind.value}"))
        return _apply(row, comparator(spec, ts_value, booking.value, ctx))


def _raw_display(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return " | ".join(str(item) for item in raw)
    return str(raw)


def _attach_extraction(row: ReconciliationResult, extracted: FieldExtraction, label: str | None = None) -> None:
    """Copy a field's extraction (value, evidence, notes) onto a row; ``label`` marks related-field context."""
    prefix = f"[{label}] " if label else ""
    if extracted.status == ExtractionStatus.EXTRACTED and label is None:
        row.term_sheet_value = display_value(extracted.value)
        row.term_sheet_raw_value = extracted.raw_value
        row.value_basis = extracted.basis
    row.evidence_quotes += [prefix + e.quote for e in extracted.evidence]
    row.evidence_pages = sorted(set(row.evidence_pages) | set(extracted.pages))
    if extracted.evidence:
        verified = all(e.verified for e in extracted.evidence)
        row.evidence_verified = verified if row.evidence_verified is None else row.evidence_verified and verified
    notes = [extracted.note] if extracted.note else []
    notes += extracted.warnings
    if notes:
        text = prefix + " | ".join(notes)
        row.extraction_note = f"{row.extraction_note} | {text}" if row.extraction_note else text


def _apply(row: ReconciliationResult, comparison: Comparison) -> ReconciliationResult:
    row.comparison_method = comparison.method
    row.initial_result = comparison.result
    row.initial_reason = comparison.reason
    row.hard_difference = comparison.hard_difference
    row.candidate_equivalence = comparison.candidate_equivalence
    if comparison.term_sheet_normalized is not None and row.term_sheet_value is None:
        row.term_sheet_value = comparison.term_sheet_normalized
    if comparison.booking_normalized is not None:
        row.booking_normalized = comparison.booking_normalized
    if comparison.result == InitialResult.MATCH:
        row.final_status, row.adjudication_source = FinalStatus.MATCH, "deterministic"
    elif comparison.result == InitialResult.NOT_COMPARABLE:
        row.final_status, row.adjudication_source = FinalStatus.UNVERIFIABLE, "deterministic"
    return row


def _parsing_error(row: ReconciliationResult, error_type: str, details: str) -> ReconciliationResult:
    row.comparison_method = "not_compared"
    row.initial_result = InitialResult.PARSING_ERROR
    row.initial_reason = "Value failed validation; not compared"
    row.final_status = FinalStatus.PARSING_ERROR
    row.adjudication_source = "deterministic"
    row.error_type = error_type
    row.error_details = details
    return row
