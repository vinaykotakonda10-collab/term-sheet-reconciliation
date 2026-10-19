"""Pydantic v2 schemas.

Two families of models live here:

* **LLM contracts** – the strict shape every extracted field and every
  mismatch adjudication must satisfy. Value types never silently coerce: a
  coupon of ``"10.75%"`` (string) where a number is required fails validation
  and is reported as a *Parsing Error*, not as a mismatch.
* **Result records** – extraction, booking and reconciliation records that are
  serialised into the reports.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Annotated, Any, Generic, Literal, TypeVar

from annotated_types import Ge, Gt, Le
from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, model_validator

from .field_catalog import AMORTIZATION_VALUES, DAY_COUNT_VALUES, FREQUENCY_VALUES, PERPETUAL, FieldKind
from .normalizer import ISIN_PATTERN, is_valid_isin, parse_decimal

SCHEMA_VERSION = "1.0"

# ISO 4217 active alphabetic codes.
ISO_4217 = frozenset(
    """AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BRL BSD BTN BWP BYN BZD
    CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF
    GTQ GYD HKD HNL HTG HUF IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP
    LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB
    PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL
    THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XOF XPF YER ZAR ZMW ZWL""".split()
)

# --------------------------------------------------------------------------- value validators


def _strict_number(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"must be a JSON number, got {type(value).__name__} {value!r}")
    return parse_decimal(value)


def _strict_iso_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise ValueError(f"must be an ISO date string 'YYYY-MM-DD', got {value!r}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a valid calendar date") from exc


def _maturity(value: Any) -> date | str:
    if value == PERPETUAL:
        return PERPETUAL
    return _strict_iso_date(value)


def _isin(value: str) -> str:
    if not ISIN_PATTERN.fullmatch(value):
        raise ValueError(f"{value!r} is not a 12-character ISIN (2 letters, 9 alphanumerics, 1 check digit, no spaces)")
    if not is_valid_isin(value):
        raise ValueError(f"{value!r} fails the ISIN check-digit test")
    return value


def _currency(value: str) -> str:
    if value not in ISO_4217:
        raise ValueError(f"{value!r} is not an ISO 4217 currency code")
    return value


NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
IsinValue = Annotated[str, AfterValidator(_isin)]
IsoDate = Annotated[date, BeforeValidator(_strict_iso_date)]
MaturityValue = Annotated[date | Literal["PERPETUAL"], BeforeValidator(_maturity)]
PercentValue = Annotated[Decimal, BeforeValidator(_strict_number), Ge(0), Le(100)]
PriceValue = Annotated[Decimal, BeforeValidator(_strict_number), Gt(0), Le(200)]
AmountValue = Annotated[Decimal, BeforeValidator(_strict_number), Gt(0)]
CurrencyValue = Annotated[str, AfterValidator(_currency)]
LocationsValue = Annotated[list[NonEmptyText], Field(min_length=1)]

VALUE_TYPES: dict[FieldKind, Any] = {
    FieldKind.TEXT: NonEmptyText,
    FieldKind.ISIN: IsinValue,
    FieldKind.DATE: IsoDate,
    FieldKind.MATURITY: MaturityValue,
    FieldKind.PERCENT: PercentValue,
    FieldKind.PRICE: PriceValue,
    FieldKind.AMOUNT: AmountValue,
    FieldKind.CURRENCY: CurrencyValue,
    FieldKind.FREQUENCY: Literal[FREQUENCY_VALUES],  # type: ignore[valid-type]
    FieldKind.DAY_COUNT: Literal[DAY_COUNT_VALUES],  # type: ignore[valid-type]
    FieldKind.BUSINESS_DAY_CONVENTION: NonEmptyText,
    FieldKind.LOCATIONS: LocationsValue,
    FieldKind.AMORTIZATION: Literal[AMORTIZATION_VALUES],  # type: ignore[valid-type]
}

# --------------------------------------------------------------------------- LLM extraction contract

V = TypeVar("V")


def _advisory_page(value: Any) -> int | None:
    # The page cited by the LLM is advisory: the pipeline locates every quote
    # itself, so an unusable page number is dropped instead of failing the field.
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 1:
        return value
    if isinstance(value, str) and value.strip().isdigit() and int(value) >= 1:
        return int(value)
    return None


class EvidenceQuote(BaseModel):
    model_config = ConfigDict(extra="ignore")

    quote: Annotated[str, StringConstraints(min_length=1, max_length=2000)]
    page: Annotated[int | None, BeforeValidator(_advisory_page)] = None


class ExtractedField(BaseModel, Generic[V]):
    """One field as returned by the extraction LLM call."""

    model_config = ConfigDict(extra="ignore")

    value: V | None = None
    raw_value: str | None = None
    evidence: list[EvidenceQuote] = Field(default_factory=list)
    basis: Literal["stated", "derived"] | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _consistency(self) -> "ExtractedField[V]":
        if self.value is not None:
            if not self.evidence:
                raise ValueError("a non-null value must be supported by at least one evidence quote")
            if self.basis is None:
                raise ValueError("basis must be 'stated' or 'derived' when a value is given")
            if self.basis == "derived" and not (self.note and self.note.strip()):
                raise ValueError("a derived value must explain its derivation in 'note'")
        return self


class AdjudicationResponse(BaseModel):
    """Contract for the second-pass (mismatch analysis) LLM call."""

    model_config = ConfigDict(extra="ignore")

    verdict: Literal["TRUE_MISMATCH", "FALSE_ALERT", "INSUFFICIENT_EVIDENCE"]
    category: Literal[
        "FORMAT_DIFFERENCE",
        "UNIT_OR_SCALE_DIFFERENCE",
        "EQUIVALENT_TERMINOLOGY",
        "BUSINESS_DAY_ADJUSTMENT",
        "ROUNDING",
        "VALUE_DIFFERENCE",
        "SEMANTIC_DIFFERENCE",
        "MISSING_VALUE",
        "OTHER",
    ]
    explanation: Annotated[str, StringConstraints(strip_whitespace=True, min_length=10, max_length=2000)]
    supporting_evidence: list[Annotated[str, StringConstraints(min_length=1, max_length=1000)]] = Field(
        default_factory=list
    )
    confidence: Literal["high", "medium", "low"]


# --------------------------------------------------------------------------- result records


class FinalStatus(str, Enum):
    MATCH = "Match"
    FALSE_ALERT = "False Alert"
    TRUE_MISMATCH = "True Mismatch"
    PARSING_ERROR = "Parsing Error"
    UNVERIFIABLE = "Unverifiable"
    UNRESOLVED = "Unresolved"


class InitialResult(str, Enum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    NOT_COMPARABLE = "NOT_COMPARABLE"
    PARSING_ERROR = "PARSING_ERROR"


class ExtractionStatus(str, Enum):
    EXTRACTED = "EXTRACTED"
    NOT_IN_DOCUMENT = "NOT_IN_DOCUMENT"
    PARSING_ERROR = "PARSING_ERROR"


class VerifiedEvidence(BaseModel):
    quote: str
    claimed_page: int | None = None
    page: int | None = None
    verified: bool
    match_type: str | None = None  # "normalized" | "whitespace_insensitive"
    text_view: str | None = None  # "layout" | "reading_order"


class FieldExtraction(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    field: str
    status: ExtractionStatus
    value: Any = None
    raw_value: str | None = None
    basis: str | None = None
    note: str | None = None
    evidence: list[VerifiedEvidence] = Field(default_factory=list)
    grounding: str | None = None  # GROUNDED | NOT_GROUNDED | NOT_APPLICABLE
    error_type: str | None = None
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    llm_output: Any = None

    @property
    def pages(self) -> list[int]:
        return sorted({e.page for e in self.evidence if e.page is not None})


class DocumentExtraction(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    document: str
    document_path: str
    sha256: str
    file_type: str
    page_count: int
    model: str
    prompt_version: str
    schema_version: str = SCHEMA_VERSION
    extracted_at: str
    status: Literal["OK", "FAILED"]
    error: str | None = None
    strategy: str | None = None
    pages_sent: list[int] = Field(default_factory=list)
    chars_sent: int = 0
    field_coverage: dict[str, bool] = Field(default_factory=dict)
    llm_calls: int = 0
    from_cache: bool = False
    repair_attempted: bool = False
    repaired_fields: list[str] = Field(default_factory=list)
    fields: dict[str, FieldExtraction] = Field(default_factory=dict)


class BookingValue(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    raw: Any = None
    value: Any = None
    error: str | None = None

    @property
    def is_missing(self) -> bool:
        return self.error is None and self.value is None


class BookingTrade(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    trade_id: str
    isin: str | None
    source_file: str
    source_format: str
    record_number: int
    values: dict[str, BookingValue]


class ReconciliationResult(BaseModel):
    """One row of the reconciliation report: one field of one trade."""

    document: str
    trade_id: str
    isin: str | None
    field: str
    field_scope: str
    term_sheet_value: str | None = None
    term_sheet_raw_value: str | None = None
    booking_value: str | None = None
    booking_normalized: str | None = None
    evidence_quotes: list[str] = Field(default_factory=list)
    evidence_pages: list[int] = Field(default_factory=list)
    evidence_verified: bool | None = None
    value_basis: str | None = None
    extraction_note: str | None = None
    comparison_method: str = ""
    initial_result: InitialResult
    initial_reason: str = ""
    hard_difference: bool = False
    candidate_equivalence: str | None = None
    final_status: FinalStatus | None = None
    llm_verdict: str | None = None
    llm_category: str | None = None
    llm_explanation: str | None = None
    llm_evidence: list[str] = Field(default_factory=list)
    llm_confidence: str | None = None
    adjudication_source: str | None = None  # deterministic | llm | llm_reused | cache
    error_type: str | None = None
    error_details: str | None = None
    booking_source: str = ""
