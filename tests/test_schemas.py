from datetime import date
from decimal import Decimal

import pytest
from pydantic import TypeAdapter, ValidationError

from src.field_catalog import FieldKind
from src.schemas import VALUE_TYPES, AdjudicationResponse, ExtractedField


def validate(kind: FieldKind, payload: dict):
    return TypeAdapter(ExtractedField[VALUE_TYPES[kind]]).validate_python(payload)


def field(value, basis="stated", note=None, quotes=("q",)):
    return {"value": value, "raw_value": None, "evidence": [{"quote": q, "page": 1} for q in quotes],
            "basis": basis, "note": note}


def test_valid_values_are_normalised_to_python_types():
    assert validate(FieldKind.PERCENT, field(10.75)).value == Decimal("10.75")
    assert validate(FieldKind.DATE, field("2025-10-14")).value == date(2025, 10, 14)
    assert validate(FieldKind.MATURITY, field("PERPETUAL")).value == "PERPETUAL"
    assert validate(FieldKind.MATURITY, field("2025-10-14")).value == date(2025, 10, 14)
    assert validate(FieldKind.ISIN, field("NO0010894330")).value == "NO0010894330"
    assert validate(FieldKind.LOCATIONS, field(["Oslo", "New York"])).value == ["Oslo", "New York"]
    assert validate(FieldKind.FREQUENCY, field("Semi-annual")).value == "Semi-annual"


@pytest.mark.parametrize(
    "kind,value",
    [
        (FieldKind.PERCENT, "10.75%"),        # string where a number is required
        (FieldKind.PERCENT, "10.75"),         # numeric string is still a string
        (FieldKind.PERCENT, 150),             # out of range
        (FieldKind.AMOUNT, -5),               # amounts must be positive
        (FieldKind.AMOUNT, True),             # booleans are not numbers
        (FieldKind.DATE, "14 October 2020"),  # not ISO
        (FieldKind.DATE, "2020-02-30"),       # not a calendar date
        (FieldKind.MATURITY, "Perpetual"),    # wrong literal
        (FieldKind.ISIN, "NO 0010894330"),    # spaces must be removed
        (FieldKind.ISIN, "NO0010894331"),     # bad check digit
        (FieldKind.CURRENCY, "RS"),           # not ISO 4217
        (FieldKind.FREQUENCY, "Yearly"),      # not an allowed enum value
        (FieldKind.LOCATIONS, []),            # empty list
        (FieldKind.LOCATIONS, "Oslo"),        # string instead of list
    ],
)
def test_invalid_types_raise_validation_errors(kind, value):
    with pytest.raises(ValidationError):
        validate(kind, field(value))


def test_consistency_rules():
    with pytest.raises(ValidationError, match="evidence"):
        validate(FieldKind.PERCENT, field(10.75, quotes=()))
    with pytest.raises(ValidationError, match="basis"):
        validate(FieldKind.PERCENT, field(10.75, basis=None))
    with pytest.raises(ValidationError, match="derivation"):
        validate(FieldKind.AMOUNT, field(5000000, basis="derived"))
    assert validate(FieldKind.AMOUNT, field(5000000, basis="derived", note="5 bonds x 1,000,000")).value == 5000000


def test_null_value_is_valid_without_evidence():
    parsed = validate(FieldKind.ISIN, {"value": None, "raw_value": None, "evidence": [], "basis": None, "note": None})
    assert parsed.value is None


def test_advisory_page_never_fails_a_field():
    parsed = validate(FieldKind.PERCENT, {"value": 1.5, "basis": "stated",
                                          "evidence": [{"quote": "1.5%", "page": "n/a"}]})
    assert parsed.evidence[0].page is None


def test_adjudication_schema():
    ok = AdjudicationResponse.model_validate({
        "verdict": "FALSE_ALERT", "category": "FORMAT_DIFFERENCE",
        "explanation": "10.75% and 0.1075 are the same rate.", "supporting_evidence": ["10.75% p.a."],
        "confidence": "high"})
    assert ok.verdict == "FALSE_ALERT"
    with pytest.raises(ValidationError):
        AdjudicationResponse.model_validate({"verdict": "MAYBE", "category": "OTHER", "explanation": "x" * 20,
                                             "confidence": "high"})
