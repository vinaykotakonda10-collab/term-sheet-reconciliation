from datetime import date
from decimal import Decimal

import pytest

from src.normalizer import (
    canonical_amortization,
    canonical_business_day_convention,
    canonical_currency,
    canonical_day_count,
    canonical_frequency,
    find_amounts_in_text,
    find_dates_in_text,
    find_percentages_in_text,
    format_decimal,
    is_valid_isin,
    normalize_entity_name,
    normalize_for_match,
    normalize_isin,
    normalize_locations,
    numeric_date_order_hint,
    parse_date,
    parse_decimal,
)


@pytest.mark.parametrize("isin", ["NO0010894330", "INE008A08U84", "US0378331005"])
def test_valid_isins_pass_check_digit(isin):
    assert is_valid_isin(isin)


@pytest.mark.parametrize("isin", ["NO0010894331", "INE008A08U8", "no0010894330", "NO 0010894330"])
def test_invalid_isins(isin):
    assert not is_valid_isin(isin)


def test_normalize_isin_removes_spaces():
    assert normalize_isin("NO 0010894330") == "NO0010894330"


@pytest.mark.parametrize(
    "text,order,expected",
    [
        ("2025-10-14", None, date(2025, 10, 14)),
        ("10/14/2025", None, date(2025, 10, 14)),  # unambiguous month-first
        ("14/10/2025", None, date(2025, 10, 14)),  # unambiguous day-first
        ("4/14/2021", "MDY", date(2021, 4, 14)),
        ("10/12/2025", "DMY", date(2025, 12, 10)),
        ("14 October 2020", None, date(2020, 10, 14)),
        ("October 17, 2014", None, date(2014, 10, 17)),
        ("17th Oct 2014", None, date(2014, 10, 17)),
    ],
)
def test_parse_date_formats(text, order, expected):
    assert parse_date(text, date_order=order) == expected


def test_parse_date_rejects_ambiguous_contradictory_and_invalid():
    with pytest.raises(ValueError, match="ambiguous"):
        parse_date("10/12/2025")
    with pytest.raises(ValueError, match="contradicts"):
        parse_date("14/10/2025", date_order="MDY")
    with pytest.raises(ValueError, match="valid calendar date"):
        parse_date("2025-02-30")
    with pytest.raises(ValueError):
        parse_date(20251014)


def test_numeric_date_order_hint():
    assert numeric_date_order_hint("4/14/2021") == "MDY"
    assert numeric_date_order_hint("14/4/2021") == "DMY"
    assert numeric_date_order_hint("4/5/2021") is None


def test_find_dates_in_text():
    found = find_dates_in_text("Deemed Date of Allotment October 17, 2014 and 14 April 2021")
    assert date(2014, 10, 17) in found and date(2021, 4, 14) in found


def test_parse_decimal_is_exact_and_strict():
    assert parse_decimal(10.75) == Decimal("10.75")
    assert parse_decimal("15,000,000,000") == Decimal("15000000000")
    assert format_decimal(parse_decimal("11.00")) == "11"
    for bad in ("10.75%", "abc", True, None, float("nan")):
        with pytest.raises(ValueError):
            parse_decimal(bad)


def test_amount_extraction_handles_indian_units_and_grouping():
    assert Decimal("15000000000") in find_amounts_in_text("Rs.1,500 Crores with an option")
    assert Decimal("1000000") in find_amounts_in_text("Rs.10,00,000/- (Rupees Ten Lakh) per Bond.")
    assert Decimal("300000000") in find_amounts_in_text("USD 300,000,000")
    assert Decimal("2500000") in find_amounts_in_text("USD 2.5 million")


def test_percentages():
    assert find_percentages_in_text("10.75% p.a. and 97 per cent") == [Decimal("10.75"), Decimal("97")]


@pytest.mark.parametrize("text,expected", [("Annual", "Annual"), ("annually in arrear", "Annual"),
                                           ("Semi-annual", "Semi-annual"), ("half-yearly", "Semi-annual"),
                                           ("fortnightly", None)])
def test_canonical_frequency(text, expected):
    assert canonical_frequency(text) == expected


@pytest.mark.parametrize("text,expected", [("Actual/Actual", "Actual/Actual"), ("Act/Act", "Actual/Actual"),
                                           ("Actual/ Actual", "Actual/Actual"), ("30/360", "30/360"),
                                           ("ACT/365 (Fixed)", "Actual/365 (Fixed)"), ("weird", None)])
def test_canonical_day_count(text, expected):
    assert canonical_day_count(text) == expected


def test_canonical_business_day_convention():
    assert canonical_business_day_convention("unadjusted") == "Unadjusted"
    assert canonical_business_day_convention("Modified Following") == "Modified Following"
    assert canonical_business_day_convention("Following for coupons; Preceding otherwise") is None
    assert canonical_business_day_convention("Business day adjustment") is None


def test_other_vocabularies():
    assert canonical_amortization("bullet") == "Bullet"
    assert canonical_amortization("Perpetual") == "Perpetual"
    assert canonical_currency("Rs.") == "INR" and canonical_currency("usd") == "USD"
    assert normalize_locations("Mumbai|New Delhi") == ["Mumbai", "New Delhi"]
    assert normalize_locations(["Oslo", " New York "]) == ["Oslo", "New York"]


def test_text_normalisation():
    assert normalize_entity_name("IDBI Bank Ltd.") == normalize_entity_name("idbi bank limited")
    assert normalize_for_match("Issuer‟s  “Bonds” \n write–down") == "Issuer's 'Bonds' write-down"
