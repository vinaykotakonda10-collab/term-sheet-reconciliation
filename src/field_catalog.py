"""Catalogue of reconciled fields.

Each field declares its data kind (which drives schema validation and the
comparison rule), whether it describes the whole instrument or an individual
trade, and the business definition used both in the LLM prompt and in reports.
Adding a field means adding one ``FieldSpec`` here; the schema, prompt and
reconciliation engine pick it up automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class FieldKind(str, Enum):
    TEXT = "text"
    ISIN = "isin"
    DATE = "date"
    MATURITY = "maturity"
    PERCENT = "percent"
    PRICE = "price"
    AMOUNT = "amount"
    CURRENCY = "currency"
    FREQUENCY = "frequency"
    DAY_COUNT = "day_count"
    BUSINESS_DAY_CONVENTION = "business_day_convention"
    LOCATIONS = "locations"
    AMORTIZATION = "amortization"


class FieldScope(str, Enum):
    INSTRUMENT = "instrument"  # same for every trade in the bond (coupon, maturity, ...)
    TRADE = "trade"  # specific to one booking (notional)


FREQUENCY_VALUES = ("Annual", "Semi-annual", "Quarterly", "Monthly", "At maturity")
DAY_COUNT_VALUES = (
    "Actual/Actual",
    "Actual/Actual (ICMA)",
    "Actual/Actual (ISDA)",
    "Actual/365 (Fixed)",
    "Actual/360",
    "30/360",
    "30E/360",
)
AMORTIZATION_VALUES = ("Bullet", "Amortizing", "Sinking fund", "Perpetual")
PERPETUAL = "PERPETUAL"


@dataclass(frozen=True)
class FieldSpec:
    name: str
    kind: FieldKind
    scope: FieldScope
    description: str

    @property
    def json_type(self) -> str:
        return _JSON_TYPE_HINTS[self.kind]


_JSON_TYPE_HINTS = {
    FieldKind.TEXT: "string",
    FieldKind.ISIN: "string (12 chars, no spaces)",
    FieldKind.DATE: 'string "YYYY-MM-DD"',
    FieldKind.MATURITY: f'string "YYYY-MM-DD" or "{PERPETUAL}"',
    FieldKind.PERCENT: "number (percent units)",
    FieldKind.PRICE: "number (percent of par)",
    FieldKind.AMOUNT: "number (full currency units)",
    FieldKind.CURRENCY: "string (ISO 4217 code)",
    FieldKind.FREQUENCY: "one of " + " | ".join(f'"{v}"' for v in FREQUENCY_VALUES),
    FieldKind.DAY_COUNT: "one of " + " | ".join(f'"{v}"' for v in DAY_COUNT_VALUES),
    FieldKind.BUSINESS_DAY_CONVENTION: "string",
    FieldKind.LOCATIONS: "array of strings",
    FieldKind.AMORTIZATION: "one of " + " | ".join(f'"{v}"' for v in AMORTIZATION_VALUES),
}


FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "ISIN", FieldKind.ISIN, FieldScope.INSTRUMENT,
        "ISIN of THIS bond, returned without spaces (\"NO 0010894330\" -> \"NO0010894330\"). "
        "Ignore identifiers of other instruments (e.g. a bond being refinanced). Null if not stated.",
    ),
    FieldSpec(
        "Issuer", FieldKind.TEXT, FieldScope.INSTRUMENT,
        "Legal name of the issuing entity as written (without registration details).",
    ),
    FieldSpec(
        "Parent", FieldKind.TEXT, FieldScope.INSTRUMENT,
        "Parent / holding company of the issuer, only if the document explicitly names one. "
        "Guarantors, trustees or arrangers are not the parent. Null if not stated.",
    ),
    FieldSpec(
        "Maturity", FieldKind.MATURITY, FieldScope.INSTRUMENT,
        f"Final maturity / redemption date. Return \"{PERPETUAL}\" if the bond is perpetual (no fixed maturity). "
        "Call dates are not maturity dates.",
    ),
    FieldSpec(
        "Notional", FieldKind.AMOUNT, FieldScope.TRADE,
        "Notional of ONE specific trade. A term sheet describes the whole issue, so return null unless the "
        "document explicitly labels an amount as a specific trade's notional (e.g. a trade confirmation). The "
        "nominal amount per bond, the issue amount and the minimum subscription are NEVER a notional.",
    ),
    FieldSpec(
        "Coupon", FieldKind.PERCENT, FieldScope.INSTRUMENT,
        "Annual coupon / interest rate of the bonds in percent units (\"9.25% p.a.\" -> 9.25). "
        "Not penal, default or application-money interest.",
    ),
    FieldSpec(
        "Currency", FieldKind.CURRENCY, FieldScope.INSTRUMENT,
        "ISO 4217 code of the bond's currency (\"Rs.\"/\"Rupees\" -> \"INR\", \"USD\" -> \"USD\").",
    ),
    FieldSpec(
        "SettlementDate", FieldKind.DATE, FieldScope.INSTRUMENT,
        "Date the bonds settle (delivery against payment) if the document states one. Return null if only a "
        "date range is given or no settlement date is stated; do not copy the issue date unless the document "
        "says they are the same.",
    ),
    FieldSpec(
        "IssueDate", FieldKind.DATE, FieldScope.INSTRUMENT,
        "Date the bonds are issued / allotted (e.g. \"Issue Date\", \"Deemed Date of Allotment\"). "
        "Not the subscription opening or closing date.",
    ),
    FieldSpec(
        "IssueAmount", FieldKind.AMOUNT, FieldScope.INSTRUMENT,
        "Total size of the whole issue in full currency units (Crore = 10,000,000; Lakh = 100,000). "
        "If a base size plus an over-subscription / green-shoe option is given, return the base size and "
        "mention the option in note. This is NOT a trade notional.",
    ),
    FieldSpec(
        "IssuePrice", FieldKind.PRICE, FieldScope.INSTRUMENT,
        "Issue price as a percentage of nominal / par (\"97% of the Nominal Amount\" -> 97). Null if not stated.",
    ),
    FieldSpec(
        "NominalAmountPerBond", FieldKind.AMOUNT, FieldScope.INSTRUMENT,
        "Face value / nominal amount / denomination of ONE bond in full currency units.",
    ),
    FieldSpec(
        "InterestPaymentDate", FieldKind.DATE, FieldScope.INSTRUMENT,
        "FIRST scheduled interest / coupon payment date. If only a rule is given (e.g. \"on the anniversary of "
        "the Deemed Date of Allotment\"), derive the first date from explicitly stated dates (basis \"derived\").",
    ),
    FieldSpec(
        "InterestPaymentFrequency", FieldKind.FREQUENCY, FieldScope.INSTRUMENT,
        "How often coupons are paid.",
    ),
    FieldSpec(
        "DayCountFraction", FieldKind.DAY_COUNT, FieldScope.INSTRUMENT,
        "Day count convention used to accrue the bonds' coupon. Quote the clause that governs coupon / interest "
        "computation on the bonds, not one for ancillary amounts (e.g. interest on application money).",
    ),
    FieldSpec(
        "BusinessDayConvention", FieldKind.BUSINESS_DAY_CONVENTION, FieldScope.INSTRUMENT,
        "Business day convention, summarised concisely (e.g. \"Unadjusted\", \"Following\", \"Modified "
        "Following\", \"Preceding\"). If different dates use different conventions, describe each briefly.",
    ),
    FieldSpec(
        "BusinessDayLocation", FieldKind.LOCATIONS, FieldScope.INSTRUMENT,
        "Business-day financial centres explicitly stated for payments (e.g. [\"Oslo\", \"New York\"]). "
        "Do not infer from governing law, courts, listing venue or addresses. Null if not stated.",
    ),
    FieldSpec(
        "AmortizationType", FieldKind.AMORTIZATION, FieldScope.INSTRUMENT,
        "Principal repayment profile: Bullet (repaid in full at maturity), Amortizing, Sinking fund, or "
        "Perpetual (no fixed redemption).",
    ),
    FieldSpec(
        "MinimumSubscription", FieldKind.AMOUNT, FieldScope.INSTRUMENT,
        "Minimum subscription / application amount in full currency units. If expressed as a number of bonds, "
        "multiply by the face value (basis \"derived\").",
    ),
)

FIELD_NAMES: tuple[str, ...] = tuple(spec.name for spec in FIELD_SPECS)
SPEC_BY_NAME: dict[str, FieldSpec] = {spec.name: spec for spec in FIELD_SPECS}
