"""Booking-system extract ingestion (CSV and JSON).

* Column names are normalised and mapped to the canonical field names
  (``trade_id``, ``Trade ID`` and ``TradeID`` are the same column).
* Every value is parsed into the same normalised types the term-sheet side
  uses; a value that cannot be parsed is kept with an error and reported as a
  booking-side *Parsing Error* for that field only.
* Numeric dates such as ``4/14/2021`` are parsed with a day/month order that is
  inferred for the whole file from unambiguous values (or configured via
  ``BOOKING_DATE_ORDER``). Ambiguous files fail loudly instead of guessing.
* When both CSV and JSON extracts are loaded, the trades are reconciled once
  (JSON is the primary, typed source) and a CSV/JSON parity report is produced.
"""

from __future__ import annotations

import csv
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .field_catalog import FIELD_NAMES, PERPETUAL, SPEC_BY_NAME, FieldKind, FieldSpec
from .normalizer import (
    canonical_currency,
    collapse_whitespace,
    display_value,
    is_valid_isin,
    normalize_isin,
    normalize_locations,
    numeric_date_order_hint,
    parse_date,
    parse_decimal,
)
from .schemas import ISO_4217, BookingTrade, BookingValue

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ("TradeID", "ISIN")
DATE_KINDS = (FieldKind.DATE, FieldKind.MATURITY)


class BookingDataError(Exception):
    """The booking file is missing, unreadable or structurally invalid."""


def _column_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


_COLUMN_ALIASES = {_column_key(name): name for name in ("TradeID",) + FIELD_NAMES}
_COLUMN_ALIASES.update(
    {
        "trade": "TradeID", "tradenumber": "TradeID", "tradeidentifier": "TradeID",
        "issuername": "Issuer", "maturitydate": "Maturity", "notionalamount": "Notional",
        "couponrate": "Coupon", "ccy": "Currency", "settledate": "SettlementDate",
        "settlement": "SettlementDate", "daycount": "DayCountFraction", "daycountconvention": "DayCountFraction",
        "facevalue": "NominalAmountPerBond", "denomination": "NominalAmountPerBond",
        "firstinterestpaymentdate": "InterestPaymentDate", "couponfrequency": "InterestPaymentFrequency",
        "minimumsubscriptionamount": "MinimumSubscription", "parentcompany": "Parent",
    }
)


def canonical_column(name: str) -> str | None:
    return _COLUMN_ALIASES.get(_column_key(name))


@dataclass
class BookingDataset:
    path: Path
    format: str
    trades: list[BookingTrade]
    date_order: str | None
    warnings: list[str] = field(default_factory=list)


@dataclass
class ParityReport:
    csv_file: str
    json_file: str
    consistent: bool
    only_in_csv: list[str]
    only_in_json: list[str]
    differences: list[dict]

    def as_dict(self) -> dict:
        return {
            "csv_file": self.csv_file,
            "json_file": self.json_file,
            "consistent": self.consistent,
            "only_in_csv": self.only_in_csv,
            "only_in_json": self.only_in_json,
            "differences": self.differences,
        }


@dataclass
class BookingLoadResult:
    trades: list[BookingTrade]
    source: str  # human-readable description of the records reconciled
    parity: ParityReport | None
    warnings: list[str]


# --------------------------------------------------------------------------- readers


def _read_csv(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise BookingDataError(f"{path.name}: CSV has no header row")
            rows = []
            for row in reader:
                if None in row:
                    raise BookingDataError(f"{path.name}: line {reader.line_num} has more values than header columns")
                if any((v or "").strip() for v in row.values()):
                    rows.append(row)
            return rows
    except UnicodeDecodeError as exc:
        raise BookingDataError(f"{path.name}: file is not UTF-8 text ({exc})") from exc
    except csv.Error as exc:
        raise BookingDataError(f"{path.name}: malformed CSV ({exc})") from exc


def _read_json(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise BookingDataError(f"{path.name}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    records = payload.get("trades") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        raise BookingDataError(f'{path.name}: expected a list of trades or an object with a "trades" list')
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise BookingDataError(f"{path.name}: trade #{index} is not a JSON object")
    return records


# --------------------------------------------------------------------------- normalisation


def _resolve_date_order(records: list[dict[str, Any]], configured: str, path: Path) -> str | None:
    date_fields = [name for name in FIELD_NAMES if SPEC_BY_NAME[name].kind in DATE_KINDS]
    numeric_dates = [
        str(record[name]) for record in records for name in date_fields
        if isinstance(record.get(name), str) and re.fullmatch(r"\s*\d{1,2}[/.\-]\d{1,2}[/.\-]\d{4}\s*", record[name])
    ]
    hints = {h for h in (numeric_date_order_hint(v) for v in numeric_dates) if h}
    if len(hints) > 1:
        raise BookingDataError(f"{path.name}: numeric dates mix month-first and day-first formats")
    inferred = next(iter(hints), None)
    if configured != "auto":
        if inferred and inferred != configured:
            raise BookingDataError(
                f"{path.name}: BOOKING_DATE_ORDER={configured} contradicts dates in the file, which are {inferred}"
            )
        return configured
    if numeric_dates and inferred is None:
        raise BookingDataError(
            f"{path.name}: numeric dates are ambiguous (day and month both <= 12); set BOOKING_DATE_ORDER=MDY or DMY"
        )
    return inferred


def normalize_booking_value(spec: FieldSpec, raw: Any, date_order: str | None) -> BookingValue:
    if raw is None or (isinstance(raw, str) and raw.strip() == ""):
        return BookingValue(raw=raw, value=None)
    try:
        value = _normalize(spec.kind, raw, date_order)
    except ValueError as exc:
        return BookingValue(raw=raw, value=None, error=str(exc))
    return BookingValue(raw=raw, value=value)


def _normalize(kind: FieldKind, raw: Any, date_order: str | None) -> Any:
    if kind in (FieldKind.TEXT, FieldKind.BUSINESS_DAY_CONVENTION, FieldKind.FREQUENCY,
                FieldKind.DAY_COUNT, FieldKind.AMORTIZATION):
        if not isinstance(raw, str):
            raise ValueError(f"expected text, got {type(raw).__name__} {raw!r}")
        return collapse_whitespace(raw)
    if kind == FieldKind.ISIN:
        if not isinstance(raw, str):
            raise ValueError(f"expected an ISIN string, got {type(raw).__name__}")
        isin = normalize_isin(raw)
        if not is_valid_isin(isin):
            raise ValueError(f"{raw!r} is not a valid ISIN")
        return isin
    if kind == FieldKind.MATURITY and isinstance(raw, str) and raw.strip().lower() in ("perpetual", "undated"):
        return PERPETUAL
    if kind in DATE_KINDS:
        return parse_date(raw, date_order=date_order)
    if kind in (FieldKind.PERCENT, FieldKind.PRICE):
        if isinstance(raw, str):
            raw = raw.strip().removesuffix("%").strip()
        return parse_decimal(raw)
    if kind == FieldKind.AMOUNT:
        return parse_decimal(raw)
    if kind == FieldKind.CURRENCY:
        if not isinstance(raw, str):
            raise ValueError(f"expected a currency code, got {type(raw).__name__}")
        code = canonical_currency(raw)
        if code is None or code not in ISO_4217:
            raise ValueError(f"{raw!r} is not a recognised currency")
        return code
    if kind == FieldKind.LOCATIONS:
        locations = normalize_locations(raw)
        if not locations:
            raise ValueError("empty location list")
        return locations
    raise ValueError(f"unsupported field kind {kind}")


def _trade_id(raw: Any) -> str:
    if isinstance(raw, bool) or raw is None:
        raise ValueError("missing TradeID")
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    text = str(raw).strip()
    if not text:
        raise ValueError("missing TradeID")
    return str(int(text)) if text.isdigit() else text


# --------------------------------------------------------------------------- public API


def load_booking_file(path: str | Path, *, date_order: str = "auto") -> BookingDataset:
    path = Path(path)
    if not path.exists():
        raise BookingDataError(f"Booking file not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        records, file_format = _read_csv(path), "csv"
    elif suffix == ".json":
        records, file_format = _read_json(path), "json"
    else:
        raise BookingDataError(f"{path.name}: unsupported booking format {suffix!r} (expected .csv or .json)")
    if not records:
        raise BookingDataError(f"{path.name}: no trades found")

    warnings: list[str] = []
    mapped_records: list[dict[str, Any]] = []
    unknown_columns: set[str] = set()
    for record in records:
        mapped: dict[str, Any] = {}
        for key, value in record.items():
            canonical = canonical_column(str(key))
            if canonical is None:
                unknown_columns.add(str(key))
            elif canonical in mapped:
                raise BookingDataError(f"{path.name}: columns map to the same field {canonical!r} twice")
            else:
                mapped[canonical] = value
        mapped_records.append(mapped)
    if unknown_columns:
        warnings.append(f"{path.name}: ignored unrecognised columns {sorted(unknown_columns)}")

    present = set().union(*(r.keys() for r in mapped_records))
    missing_required = [c for c in REQUIRED_COLUMNS if c not in present]
    if missing_required:
        raise BookingDataError(f"{path.name}: required column(s) missing: {missing_required}")
    missing_optional = [name for name in FIELD_NAMES if name not in present]
    if missing_optional:
        warnings.append(f"{path.name}: columns absent (treated as missing values): {missing_optional}")

    order = _resolve_date_order(mapped_records, date_order, path)
    trades: list[BookingTrade] = []
    seen: dict[str, int] = {}
    for number, record in enumerate(mapped_records, start=1):
        try:
            trade_id = _trade_id(record.get("TradeID"))
        except ValueError as exc:
            raise BookingDataError(f"{path.name}: record #{number}: {exc}") from exc
        if trade_id in seen:
            raise BookingDataError(
                f"{path.name}: duplicate TradeID {trade_id!r} (records #{seen[trade_id]} and #{number})"
            )
        seen[trade_id] = number
        values = {name: normalize_booking_value(SPEC_BY_NAME[name], record.get(name), order) for name in FIELD_NAMES}
        isin = values["ISIN"].value
        trades.append(
            BookingTrade(
                trade_id=trade_id, isin=isin, source_file=path.name, source_format=file_format,
                record_number=number, values=values,
            )
        )
        bad = {name: v.error for name, v in values.items() if v.error}
        if bad:
            warnings.append(f"{path.name}: trade {trade_id} has unparseable values {bad}")

    for message in warnings:
        logger.warning(message)
    logger.info(
        "Loaded %d trade(s) from %s (date order: %s)", len(trades), path.name, order or "n/a",
        extra={"event": "booking_loaded", "file": path.name, "trades": len(trades)},
    )
    return BookingDataset(path=path, format=file_format, trades=trades, date_order=order, warnings=warnings)


def compare_parity(csv_data: BookingDataset, json_data: BookingDataset) -> ParityReport:
    csv_trades = {(t.trade_id, t.isin): t for t in csv_data.trades}
    json_trades = {(t.trade_id, t.isin): t for t in json_data.trades}
    differences = []
    for key in sorted(csv_trades.keys() & json_trades.keys()):
        for name in FIELD_NAMES:
            a, b = csv_trades[key].values[name], json_trades[key].values[name]
            if a.value != b.value or bool(a.error) != bool(b.error):
                differences.append(
                    {"trade_id": key[0], "field": name, "csv": display_value(a.value) if a.value is not None else a.raw,
                     "json": display_value(b.value) if b.value is not None else b.raw}
                )
    only_csv = [f"{k[0]} ({k[1]})" for k in sorted(csv_trades.keys() - json_trades.keys())]
    only_json = [f"{k[0]} ({k[1]})" for k in sorted(json_trades.keys() - csv_trades.keys())]
    return ParityReport(
        csv_file=csv_data.path.name, json_file=json_data.path.name,
        consistent=not (differences or only_csv or only_json),
        only_in_csv=only_csv, only_in_json=only_json, differences=differences,
    )


def load_booking_records(
    csv_path: Path | None, json_path: Path | None, *, booking_format: str = "both", date_order: str = "auto"
) -> BookingLoadResult:
    """Load the trades to reconcile according to ``booking_format`` (csv | json | both)."""
    if booking_format not in ("csv", "json", "both"):
        raise BookingDataError(f"booking format must be csv, json or both, got {booking_format!r}")
    if booking_format == "csv":
        if csv_path is None:
            raise BookingDataError("booking format 'csv' selected but no CSV file is configured")
        data = load_booking_file(csv_path, date_order=date_order)
        return BookingLoadResult(data.trades, data.path.name, None, data.warnings)
    if booking_format == "json":
        if json_path is None:
            raise BookingDataError("booking format 'json' selected but no JSON file is configured")
        data = load_booking_file(json_path, date_order=date_order)
        return BookingLoadResult(data.trades, data.path.name, None, data.warnings)

    available = [p for p in (json_path, csv_path) if p is not None]
    if not available:
        raise BookingDataError("no booking files configured")
    loaded: dict[str, BookingDataset] = {}
    warnings: list[str] = []
    for path in available:
        try:
            dataset = load_booking_file(path, date_order=date_order)
            loaded[dataset.format] = dataset
            warnings.extend(dataset.warnings)
        except BookingDataError as exc:
            warnings.append(f"could not load {Path(path).name}: {exc}")
            logger.error("Could not load %s: %s", Path(path).name, exc)
    if not loaded:
        raise BookingDataError("; ".join(warnings))

    parity = None
    if "csv" in loaded and "json" in loaded:
        parity = compare_parity(loaded["csv"], loaded["json"])
        level = logging.INFO if parity.consistent else logging.WARNING
        logger.log(level, "CSV/JSON parity for %s vs %s: %s", parity.csv_file, parity.json_file,
                   "consistent" if parity.consistent else f"{len(parity.differences)} difference(s)")
        if not parity.consistent:
            warnings.append(f"CSV/JSON extracts disagree ({len(parity.differences)} value difference(s)); "
                            "JSON used as the primary source")
    primary = loaded.get("json") or loaded["csv"]
    source = primary.path.name if parity is None else f"{primary.path.name} (parity-checked against {parity.csv_file})"
    return BookingLoadResult(primary.trades, source, parity, warnings)
