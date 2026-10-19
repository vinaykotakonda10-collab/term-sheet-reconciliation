import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from src.booking_loader import BookingDataError, compare_parity, load_booking_file, load_booking_records

from .conftest import GENEL_CSV, GENEL_JSON, IDBI_CSV, IDBI_JSON


@pytest.mark.parametrize("path", [IDBI_CSV, IDBI_JSON, GENEL_CSV, GENEL_JSON])
def test_all_five_trades_load_without_errors(path):
    data = load_booking_file(path)
    assert [t.trade_id for t in data.trades] == ["1", "2", "3", "4", "5"]
    assert not [w for w in data.warnings if "unparseable" in w]
    for trade in data.trades:
        assert all(v.error is None for v in trade.values.values())


def test_csv_types_are_normalised():
    genel = load_booking_file(GENEL_CSV)
    assert genel.date_order == "MDY"  # inferred from 4/14/2021 and 10/14/2025
    trade = genel.trades[0]
    assert trade.isin == "NO0010894330"
    assert trade.values["Maturity"].value == date(2025, 10, 14)
    assert trade.values["InterestPaymentDate"].value == date(2021, 4, 14)
    assert trade.values["Coupon"].value == Decimal("9.25")
    assert trade.values["BusinessDayLocation"].value == ["Oslo", "New York"]
    assert trade.values["Notional"].raw == "3000000"


@pytest.mark.parametrize("csv_path,json_path", [(IDBI_CSV, IDBI_JSON), (GENEL_CSV, GENEL_JSON)])
def test_csv_and_json_extracts_have_parity(csv_path, json_path):
    report = compare_parity(load_booking_file(csv_path), load_booking_file(json_path))
    assert report.consistent, report.differences


def test_both_mode_reconciles_each_trade_once_with_parity_report():
    result = load_booking_records(IDBI_CSV, IDBI_JSON, booking_format="both")
    assert len(result.trades) == 5
    assert result.parity is not None and result.parity.consistent
    assert result.trades[0].source_format == "json"


def test_parity_detects_differences(tmp_path: Path):
    records = json.loads(GENEL_JSON.read_text())
    records["trades"][0]["Coupon"] = 9.5
    altered = tmp_path / "genel.json"
    altered.write_text(json.dumps(records))
    report = compare_parity(load_booking_file(GENEL_CSV), load_booking_file(altered))
    assert not report.consistent
    assert report.differences == [{"trade_id": "1", "field": "Coupon", "csv": "9.25", "json": "9.5"}]


def test_single_format_selection():
    assert load_booking_records(GENEL_CSV, GENEL_JSON, booking_format="csv").trades[0].source_format == "csv"
    assert load_booking_records(GENEL_CSV, GENEL_JSON, booking_format="json").trades[0].source_format == "json"
    with pytest.raises(BookingDataError):
        load_booking_records(None, GENEL_JSON, booking_format="csv")


def test_column_aliases_and_invalid_values_become_field_errors(tmp_path: Path):
    path = tmp_path / "trades.csv"
    path.write_text(
        "Trade ID,isin,Coupon Rate,Maturity Date,Notional,Currency,Extra\n"
        "7,NO0010894330,abc,2025-10-14,\"3,000,000\",usd,x\n",
        encoding="utf-8",
    )
    data = load_booking_file(path)
    trade = data.trades[0]
    assert trade.trade_id == "7"
    assert trade.values["Coupon"].error and "not a number" in trade.values["Coupon"].error
    assert trade.values["Notional"].value == Decimal("3000000")
    assert trade.values["Currency"].value == "USD"
    assert trade.values["IssueDate"].is_missing
    assert any("unrecognised columns" in w for w in data.warnings)


def test_structural_errors(tmp_path: Path):
    missing = tmp_path / "missing_isin.csv"
    missing.write_text("TradeID,Coupon\n1,5\n")
    with pytest.raises(BookingDataError, match="required column"):
        load_booking_file(missing)

    dup = tmp_path / "dup.json"
    dup.write_text(json.dumps([{"TradeID": 1, "ISIN": "NO0010894330"}, {"TradeID": "1", "ISIN": "NO0010894330"}]))
    with pytest.raises(BookingDataError, match="duplicate TradeID"):
        load_booking_file(dup)

    broken = tmp_path / "broken.json"
    broken.write_text('{"trades": [ {"TradeID": 1,, } ]}')
    with pytest.raises(BookingDataError, match="invalid JSON"):
        load_booking_file(broken)

    xml = tmp_path / "trades.xml"
    xml.write_text("<trades/>")
    with pytest.raises(BookingDataError, match="unsupported booking format"):
        load_booking_file(xml)

    with pytest.raises(BookingDataError, match="not found"):
        load_booking_file(tmp_path / "nope.csv")


def test_ambiguous_numeric_dates_require_configuration(tmp_path: Path):
    path = tmp_path / "ambiguous.csv"
    path.write_text("TradeID,ISIN,IssueDate\n1,NO0010894330,10/12/2020\n")
    with pytest.raises(BookingDataError, match="ambiguous"):
        load_booking_file(path)
    assert load_booking_file(path, date_order="DMY").trades[0].values["IssueDate"].value == date(2020, 12, 10)
    with pytest.raises(BookingDataError, match="contradicts"):
        load_booking_file(GENEL_CSV, date_order="DMY")


def test_invalid_isin_in_booking_is_a_field_error(tmp_path: Path):
    path = tmp_path / "bad_isin.json"
    path.write_text(json.dumps({"trades": [{"TradeID": 1, "ISIN": "NO0010894331"}]}))
    trade = load_booking_file(path).trades[0]
    assert trade.isin is None and "not a valid ISIN" in trade.values["ISIN"].error


def test_fallback_when_one_extract_is_unreadable(tmp_path: Path):
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    result = load_booking_records(GENEL_CSV, broken, booking_format="both")
    assert len(result.trades) == 5 and result.trades[0].source_format == "csv"
    assert any("could not load broken.json" in w for w in result.warnings)
