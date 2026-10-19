"""Deterministic normalisation of financial values.

Everything here is pure, side-effect free and unit tested. The functions are
used by schema validation, booking ingestion, evidence grounding and the
reconciliation engine, so "equivalent" means the same thing everywhere.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------- text

_QUOTE_CHARS = "\"'`´‘’‚‛“”„‟′″«»"
_DASH_CHARS = "‐‑‒–—―−"
_SPACE_CHARS = "       "
_CHAR_MAP = (
    {ord(c): "'" for c in _QUOTE_CHARS}
    | {ord(c): "-" for c in _DASH_CHARS}
    | {ord(c): " " for c in _SPACE_CHARS}
    | {0x200B: None, 0xFEFF: None, 0x00AD: None}
)


def canonical_chars(text: str) -> str:
    """NFKC-normalise and fold typographic quotes, dashes and odd spaces to ASCII.

    All quotation marks and apostrophes map to ``'`` because PDF text layers
    frequently mis-encode them (e.g. ``Issuer‟s``).
    """
    return unicodedata.normalize("NFKC", text).translate(_CHAR_MAP)


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_for_match(text: str) -> str:
    """Normalisation used to verify evidence quotes (characters + whitespace runs)."""
    return collapse_whitespace(canonical_chars(text))


def strip_all_whitespace(text: str) -> str:
    """Stricter fallback that ignores whitespace entirely (handles 'write-\\ndown' hyphenation)."""
    return re.sub(r"\s+", "", canonical_chars(text))


_LEGAL_SUFFIXES = {
    "ltd": "limited",
    "co": "company",
    "corp": "corporation",
    "inc": "incorporated",
    "pvt": "private",
    "intl": "international",
}


def normalize_entity_name(name: str) -> str:
    """Case/punctuation-insensitive entity name with common legal abbreviations expanded."""
    text = canonical_chars(name).casefold().replace("&", " and ")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(_LEGAL_SUFFIXES.get(token, token) for token in text.split())


# --------------------------------------------------------------------------- ISIN

ISIN_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def normalize_isin(text: str) -> str:
    return re.sub(r"[\s\-]", "", canonical_chars(text)).upper()


def isin_check_digit(first_eleven: str) -> int:
    """ISO 6166 check digit: letters -> numbers (A=10), then Luhn from the right."""
    digits = "".join(str(int(ch, 36)) for ch in first_eleven)
    total = 0
    for position, ch in enumerate(reversed(digits)):
        value = int(ch)
        if position % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return (10 - total % 10) % 10


def is_valid_isin(text: str) -> bool:
    return bool(ISIN_PATTERN.fullmatch(text)) and int(text[-1]) == isin_check_digit(text[:-1])


# --------------------------------------------------------------------------- dates

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
_MONTHS.update({name[:3]: num for name, num in list(_MONTHS.items())})
_MONTHS["sept"] = 9
_MONTH_ALT = "|".join(sorted(_MONTHS, key=len, reverse=True))

_ISO_DATE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})")
_NUMERIC_DATE = re.compile(r"(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{4})")
_DAY_MONTH_YEAR = re.compile(rf"(\d{{1,2}})(?:st|nd|rd|th)?[\s\-]+({_MONTH_ALT})\.?,?[\s\-]+(\d{{4}})", re.IGNORECASE)
_MONTH_DAY_YEAR = re.compile(rf"({_MONTH_ALT})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})", re.IGNORECASE)


def numeric_date_order_hint(text: str) -> str | None:
    """Return 'DMY' or 'MDY' when a numeric date such as 4/14/2021 proves its order."""
    match = _NUMERIC_DATE.fullmatch(text.strip())
    if not match:
        return None
    first, second = int(match.group(1)), int(match.group(2))
    if first > 12 >= second:
        return "DMY"
    if second > 12 >= first:
        return "MDY"
    return None


def parse_date(value: object, *, date_order: str | None = None) -> date:
    """Parse ISO, numeric (with a known day/month order) or textual dates.

    Numeric dates whose order cannot be proven from the value itself require
    ``date_order`` ("MDY" or "DMY"); otherwise a ``ValueError`` is raised rather
    than guessing.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"expected a date string, got {type(value).__name__}")
    text = collapse_whitespace(canonical_chars(value))
    try:
        if match := _ISO_DATE.fullmatch(text):
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if match := _NUMERIC_DATE.fullmatch(text):
            hint = numeric_date_order_hint(text)
            if hint and date_order and hint != date_order:
                raise ValueError(f"date {value!r} contradicts the {date_order} day/month order used by this file")
            order = hint or date_order
            if order is None:
                raise ValueError(f"ambiguous date {value!r}: day/month order is unknown")
            first, second, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
            day, month = (first, second) if order == "DMY" else (second, first)
            return date(year, month, day)
        if match := _DAY_MONTH_YEAR.fullmatch(text):
            return date(int(match.group(3)), _MONTHS[match.group(2).lower()], int(match.group(1)))
        if match := _MONTH_DAY_YEAR.fullmatch(text):
            return date(int(match.group(3)), _MONTHS[match.group(1).lower()], int(match.group(2)))
    except ValueError as exc:
        if "day is out of range" in str(exc) or "month must be" in str(exc):
            raise ValueError(f"{value!r} is not a valid calendar date") from exc
        raise
    raise ValueError(f"unrecognised date format: {value!r}")


def find_dates_in_text(text: str) -> list[date]:
    """All dates written in ``text`` (textual and ISO forms; numeric forms in both orders)."""
    text = canonical_chars(text)
    found: list[date] = []

    def _add(year: int, month: int, day: int) -> None:
        try:
            found.append(date(year, month, day))
        except ValueError:
            pass

    for m in _DAY_MONTH_YEAR.finditer(text):
        _add(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    for m in _MONTH_DAY_YEAR.finditer(text):
        _add(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
    for m in _ISO_DATE.finditer(text):
        _add(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    for m in _NUMERIC_DATE.finditer(text):
        a, b, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        _add(year, b, a)
        _add(year, a, b)
    return found


# --------------------------------------------------------------------------- numbers

_PLAIN_NUMBER = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")


def parse_decimal(value: object) -> Decimal:
    """Exact decimal from a JSON number or a numeric string (thousands separators allowed)."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        result = Decimal(repr(value))
    elif isinstance(value, str):
        text = canonical_chars(value).strip().replace(",", "").replace("_", "").replace(" ", "")
        if not _PLAIN_NUMBER.fullmatch(text):
            raise ValueError(f"not a number: {value!r}")
        result = Decimal(text)
    else:
        raise ValueError(f"expected a number, got {type(value).__name__}")
    if not result.is_finite():
        raise ValueError(f"not a finite number: {value!r}")
    return result


def format_decimal(value: Decimal) -> str:
    """Plain, exponent-free representation without trailing zeros (10.750 -> '10.75')."""
    text = format(value.normalize(), "f")
    return text


_MULTIPLIERS = {
    "crore": 10**7, "crores": 10**7, "cr": 10**7,
    "lakh": 10**5, "lakhs": 10**5, "lac": 10**5, "lacs": 10**5,
    "thousand": 10**3,
    "million": 10**6, "millions": 10**6, "mn": 10**6, "mm": 10**6,
    "billion": 10**9, "billions": 10**9, "bn": 10**9,
}
_AMOUNT = re.compile(
    r"(?<!\d)(\d{1,3}(?:,\d{2,3})+|\d+)(?:\.(\d+))?"
    r"(?:\s*(crores?|cr|lakhs?|lacs?|thousand|millions?|mn|mm|billions?|bn)\b)?",
    re.IGNORECASE,
)
_PERCENT = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:%|per\s*cent\b|percent\b)", re.IGNORECASE)


def find_amounts_in_text(text: str) -> list[Decimal]:
    """Amounts written in ``text``; a scale word (Crores, million, ...) is applied when present."""
    amounts = []
    for m in _AMOUNT.finditer(canonical_chars(text)):
        number = Decimal(m.group(1).replace(",", "") + (f".{m.group(2)}" if m.group(2) else ""))
        scale = m.group(3)
        amounts.append(number * _MULTIPLIERS[scale.lower()] if scale else number)
    return amounts


def find_percentages_in_text(text: str) -> list[Decimal]:
    return [Decimal(m.group(1)) for m in _PERCENT.finditer(canonical_chars(text))]


def decimals_equal(a: Decimal, b: Decimal) -> bool:
    return a.compare(b) == 0


# --------------------------------------------------------------------------- vocabularies


def _key(text: str) -> str:
    return collapse_whitespace(canonical_chars(text).casefold())


_FREQUENCY_SYNONYMS = {
    "Annual": ("annual", "annually", "yearly", "per annum", "once a year", "1", "12m", "1y"),
    "Semi-annual": ("semi-annual", "semiannual", "semi annual", "semi-annually", "semi annually",
                    "semiannually", "half-yearly", "half yearly", "2", "6m"),
    "Quarterly": ("quarterly", "4", "3m"),
    "Monthly": ("monthly", "12", "1m"),
    "At maturity": ("at maturity", "zero coupon", "bullet coupon"),
}
_FREQUENCY_LOOKUP = {syn: canon for canon, syns in _FREQUENCY_SYNONYMS.items() for syn in syns}


def canonical_frequency(text: str) -> str | None:
    key = _key(text)
    key = re.sub(r"\s+in\s+arrears?$", "", key)
    return _FREQUENCY_LOOKUP.get(key)


def canonical_day_count(text: str) -> str | None:
    key = re.sub(r"\s+", "", _key(text))
    key = key.replace("actual", "act").replace("fixed", "f")
    key = key.replace("(", "").replace(")", "")
    table = {
        "act/act": "Actual/Actual",
        "act/acticma": "Actual/Actual (ICMA)",
        "act/actisda": "Actual/Actual (ISDA)",
        "act/365f": "Actual/365 (Fixed)",
        "act/365": "Actual/365 (Fixed)",
        "act/360": "Actual/360",
        "30/360": "30/360",
        "30/360bondbasis": "30/360",
        "bondbasis": "30/360",
        "30u/360": "30/360",
        "30e/360": "30E/360",
        "eurobondbasis": "30E/360",
    }
    return table.get(key)


_BDC_PATTERNS = (
    ("Modified Following", r"modified\s+following"),
    ("Modified Preceding", r"modified\s+preceding"),
    ("Unadjusted", r"unadjusted|no\s+adjustment|not\s+adjusted"),
    ("Following", r"following|next\s+business\s+day|succeeding\s+business\s+day"),
    ("Preceding", r"preceding|previous\s+business\s+day"),
)


def canonical_business_day_convention(text: str) -> str | None:
    """Single canonical convention, or None when the text is unknown or mixes conventions."""
    remaining = _key(text)
    found = []
    for label, pattern in _BDC_PATTERNS:
        if re.search(pattern, remaining):
            found.append(label)
            remaining = re.sub(pattern, " ", remaining)
    return found[0] if len(found) == 1 else None


_AMORTIZATION_SYNONYMS = {
    "Bullet": ("bullet", "bullet repayment", "repaid in full at maturity", "at maturity"),
    "Amortizing": ("amortizing", "amortising", "amortization", "amortisation", "amortized", "amortised"),
    "Sinking fund": ("sinking fund",),
    "Perpetual": ("perpetual", "no maturity", "undated"),
}
_AMORTIZATION_LOOKUP = {syn: canon for canon, syns in _AMORTIZATION_SYNONYMS.items() for syn in syns}


def canonical_amortization(text: str) -> str | None:
    return _AMORTIZATION_LOOKUP.get(_key(text))


_CURRENCY_ALIASES = {
    "rs": "INR", "rs.": "INR", "inr": "INR", "rupee": "INR", "rupees": "INR", "indian rupee": "INR", "₹": "INR",
    "$": "USD", "us$": "USD", "usd": "USD", "us dollar": "USD", "us dollars": "USD",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP", "pound sterling": "GBP",
    "nok": "NOK", "norwegian krone": "NOK",
}


def canonical_currency(text: str) -> str | None:
    key = _key(text)
    if key in _CURRENCY_ALIASES:
        return _CURRENCY_ALIASES[key]
    if re.fullmatch(r"[a-z]{3}", key):
        return key.upper()
    return None


def normalize_locations(value: object) -> list[str]:
    """Split a delimited string or list of business-day centres into clean names."""
    if isinstance(value, str):
        parts = re.split(r"\s*(?:\||;|,|/|\band\b)\s*", value)
    elif isinstance(value, (list, tuple)):
        parts = [str(item) for item in value]
    else:
        raise ValueError(f"expected a list or delimited string, got {type(value).__name__}")
    cleaned = [collapse_whitespace(canonical_chars(p)).strip("'\" ") for p in parts]
    return [p for p in cleaned if p]


def location_key(locations: list[str]) -> frozenset[str]:
    return frozenset(loc.casefold() for loc in locations)


# --------------------------------------------------------------------------- display


def display_value(value: object) -> str | None:
    """Stable human-readable string for normalised values (used in reports)."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return format_decimal(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return " | ".join(str(v) for v in value)
    return str(value)


def to_decimal_or_none(value: object) -> Decimal | None:
    try:
        return parse_decimal(value)
    except (ValueError, InvalidOperation):
        return None
